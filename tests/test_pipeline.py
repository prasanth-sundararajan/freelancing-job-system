import pytest

from conftest import PORTFOLIO_URL, est, proposal, qual
from freelance_agents import guardrails as g
from freelance_agents.agents import ScoutAgent
from freelance_agents.orchestrator import Orchestrator

PRICES = {"freelancer:88231": 150, "upwork:66010": 400}


def happy_responses():
    return {
        "qualifier": lambda jid: qual(jid),
        "estimator": lambda jid: est(jid, PRICES[jid]),
        "proposal_writer": lambda jid: proposal(jid, PRICES[jid], links=[PORTFOLIO_URL, "https://made-up.example/x"]),
    }


def states(ctx):
    return {r["job_id"]: r["state"] for r in ctx.db.all_jobs()}


def test_full_pipeline(make_ctx, sample_jobs):
    ctx = make_ctx(happy_responses())
    scout = ScoutAgent(ctx)
    assert scout.ingest("file", sample_jobs)["new"] == 3
    assert scout.ingest("file", sample_jobs)["duplicate"] == 3

    orch = Orchestrator(ctx)
    orch.tick()

    assert states(ctx) == {
        "freelancer:88231": "awaiting_approval",  # Freelancer prohibits auto-bidding: human queue
        "upwork:55102": "rejected",               # WhatsApp + free sample: code guardrail
        "upwork:66010": "awaiting_approval",      # no bidding API: human queue
    }
    # The red-flag job never reached a model.
    assert not any("55102" in user for _, user in ctx.llm.calls)
    # Made-up portfolio link was stripped.
    assert ctx.db.latest_output("freelancer:88231", "proposal_writer")["portfolio_links"] == [PORTFOLIO_URL]
    # Costs are tracked per job.
    assert ctx.db.get_job("freelancer:88231")["cost_usd"] > 0

    assert len(ctx.db.pending_approvals()) == 2
    orch.approve("upwork:66010")
    assert states(ctx)["upwork:66010"] == "submitted"
    assert len(ctx.db.pending_approvals()) == 1


def test_platform_that_allows_api_bidding_submits(make_ctx, sample_jobs, cfg):
    platforms = {**cfg["platforms"], "freelancer": {"api_bid_allowed": True}}
    ctx = make_ctx(happy_responses(), platforms=platforms)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    Orchestrator(ctx).tick()
    assert states(ctx)["freelancer:88231"] == "submitted"  # dry run


def test_kill_switch_halts_everything(make_ctx, sample_jobs):
    ctx = make_ctx(happy_responses(), kill_switch=True)
    ScoutAgent(ctx).ingest("file", sample_jobs)
    assert Orchestrator(ctx).tick() == {"halted": True}
    assert set(states(ctx).values()) == {"discovered"}


def test_daily_bid_limit_holds_jobs(make_ctx, sample_jobs):
    ctx = make_ctx(happy_responses(), daily_bid_limit=0)
    ScoutAgent(ctx).ingest("file", sample_jobs)
    Orchestrator(ctx).tick()
    assert states(ctx)["freelancer:88231"] == "proposed"
    assert states(ctx)["upwork:66010"] == "proposed"


def test_bad_proposal_escalates_after_retries(make_ctx, sample_jobs):
    responses = happy_responses()
    responses["proposal_writer"] = lambda jid: proposal(jid, PRICES[jid], words=40)  # always too short
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    orch = Orchestrator(ctx)
    for _ in range(3):
        orch.tick()
    assert states(ctx)["freelancer:88231"] == "escalated"


def test_low_margin_is_rejected(make_ctx, sample_jobs):
    responses = happy_responses()
    responses["estimator"] = lambda jid: est(jid, 150, task_costs=(40, 40))
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    Orchestrator(ctx).tick()
    assert states(ctx)["freelancer:88231"] == "rejected"


def test_model_cannot_override_low_score(make_ctx, sample_jobs):
    responses = happy_responses()
    responses["qualifier"] = lambda jid: qual(jid, parts=(10, 10, 10, 10))  # says "pursue" but scores 40
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    Orchestrator(ctx).tick()
    assert states(ctx)["freelancer:88231"] == "rejected"


def test_requeue_only_model_rejections(make_ctx, sample_jobs):
    responses = happy_responses()
    responses["qualifier"] = lambda jid: qual(jid, parts=(10, 10, 10, 10))  # model rejects
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs)
    Orchestrator(ctx).tick()
    assert set(states(ctx).values()) == {"rejected"}
    assert ctx.db.requeue_model_rejections() == 2  # the WhatsApp job was rejected by code, stays rejected
    assert states(ctx)["upwork:55102"] == "rejected"
    assert states(ctx)["freelancer:88231"] == "discovered"


def test_disallowed_source_refused(make_ctx, sample_jobs):
    ctx = make_ctx({})
    with pytest.raises(g.GuardrailViolation):
        ScoutAgent(ctx).ingest("scraper", sample_jobs)


def test_illegal_transition_refused(make_ctx, sample_jobs):
    ctx = make_ctx({})
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    with pytest.raises(g.GuardrailViolation):
        Orchestrator(ctx).transition("freelancer:88231", "discovered", "delivered", "skip ahead")
