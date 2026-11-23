from conftest import est, proposal, qual
from freelance_agents.agents import ScoutAgent
from freelance_agents.bid_sync import sync_bids
from freelance_agents.orchestrator import Orchestrator

PRICES = {"freelancer:88231": 150, "upwork:66010": 400}


class API:
    def __init__(self, bids):
        self.bids = bids

    def my_bids(self, pid):
        return self.bids.get(pid, [])


def queued(make_ctx, sample_jobs):
    ctx = make_ctx({"qualifier": lambda j: qual(j), "estimator": lambda j: est(j, PRICES[j]),
                    "proposal_writer": lambda j: proposal(j, PRICES[j])})
    ScoutAgent(ctx).ingest("file", sample_jobs)
    Orchestrator(ctx).tick()
    return ctx


def test_manual_bid_is_recorded(make_ctx, sample_jobs):
    ctx = queued(make_ctx, sample_jobs)
    out = sync_bids(ctx, API({88231: [{"id": 5, "award_status": None, "frontend_bid_status": "active"}]}))
    assert out["recorded"] == ["freelancer:88231"] and out["awarded"] == []
    assert ctx.db.get_job("freelancer:88231")["state"] == "submitted"
    assert ctx.db.get_job("upwork:66010")["state"] == "awaiting_approval"  # not a Freelancer job


def test_award_reported_once(make_ctx, sample_jobs):
    ctx = queued(make_ctx, sample_jobs)
    api = API({88231: [{"id": 5, "award_status": "pending", "amount": 150}]})
    first = sync_bids(ctx, api)
    assert first["awarded"] == [{"job_id": "freelancer:88231", "award_status": "pending", "amount": 150}]
    assert sync_bids(ctx, api)["awarded"] == []


def test_no_bid_nothing_changes(make_ctx, sample_jobs):
    ctx = queued(make_ctx, sample_jobs)
    assert sync_bids(ctx, API({})) == {"recorded": [], "awarded": [], "checked": 1}
