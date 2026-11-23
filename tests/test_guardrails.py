from conftest import PORTFOLIO_URL, est, proposal

from freelance_agents import guardrails as g
from freelance_agents.schemas import (
    AcceptanceCriterion,
    ClientReply,
    CriterionResult,
    DeliveryResult,
    PlanResult,
    PriceChange,
    QAIssue,
    QAResult,
    QAScores,
    TaskNode,
)


def test_prescreen_flags_red_flag_job(cfg, job_by_id):
    flags = g.prescreen_job(job_by_id["upwork:55102"], cfg)
    assert "off-platform contact or payment" in flags
    assert "unpaid test work" in flags
    assert "client payment not verified" in flags
    assert g.prescreen_job(job_by_id["freelancer:88231"], cfg) == []
    unknown = job_by_id["freelancer:88231"].model_copy(deep=True)
    unknown.client.payment_verified = None
    assert g.prescreen_job(unknown, {**cfg, "allow_unknown_payment_status": True}) == []
    assert g.prescreen_job(unknown, {**cfg, "allow_unknown_payment_status": False}) == ["client payment status unknown"]


def test_unknown_client_is_neutral_not_a_red_flag(cfg):
    from conftest import qual
    flags = [
        "payment not verified",
        "client history unknown (no rating, hires, or spend data)",
        "no client rating, hire history, or spend data available",
        "asks for a sample article before hiring",          # real flag: unpaid sample
        "off-platform contact or payment",                  # real flag
    ]
    q = qual("j", parts=(22, 23, 22, 3), flags=flags)
    out = g.enforce_qualifier(q, {**cfg, "allow_unknown_payment_status": True}, unknown_client=True)
    assert out.red_flags == ["asks for a sample article before hiring", "off-platform contact or payment"]
    assert out.breakdown.client_quality == 12
    assert out.decision == "skip"  # real flags still force skip

    clean = g.enforce_qualifier(qual("j", parts=(22, 23, 22, 3), flags=flags[:3]), {**cfg, "allow_unknown_payment_status": True}, unknown_client=True)
    assert clean.red_flags == [] and clean.score == 79 and clean.decision == "pursue"

    known = g.enforce_qualifier(qual("j", parts=(22, 23, 22, 3), flags=flags[:1]), cfg, unknown_client=False)
    assert known.red_flags == ["payment not verified"] and known.decision == "skip"


def test_estimate_recomputed_in_code(cfg, job_by_id):
    fixed, errors = g.enforce_estimate(est("j", 150), job_by_id["freelancer:88231"], cfg)
    assert fixed.total_ai_cost_usd == 13.65
    assert fixed.margin_pct == 90.9
    assert errors == []
    _, errors = g.enforce_estimate(est("j", 400), job_by_id["freelancer:88231"], cfg)
    assert any("20% above" in e for e in errors)


def inr_job(job_by_id):
    j = job_by_id["freelancer:88231"].model_copy(deep=True)
    j.budget.currency, j.budget.min, j.budget.max, j.budget.usd_rate = "INR", 12500, 37500, 0.01079
    return j


def test_estimate_in_project_currency(cfg, job_by_id):
    job = inr_job(job_by_id)
    _, errors = g.enforce_estimate(est("j", 420), job, cfg)   # priced as if USD
    assert any("far below client min" in e for e in errors)
    fixed, errors = g.enforce_estimate(est("j", 20000), job, cfg)
    assert errors == []
    assert fixed.bid_price.currency == "INR"
    assert fixed.margin_pct == round((20000 * 0.01079 - 13.65) / (20000 * 0.01079) * 100, 1)


def test_proposal_no_dollar_sign_for_non_dollar_currency(cfg, job_by_id):
    e, _ = g.enforce_estimate(est("j", 20000), inr_job(job_by_id), cfg)
    p = proposal("j", 20000, extra="Price: $20000 fixed.")
    _, errors = g.check_proposal(p, e, [])
    assert any("$ sign" in x for x in errors)
    fixed, errors = g.check_proposal(proposal("j", 20000, extra="Price: 20,000 INR fixed."), e, [])
    assert errors == [] and fixed.bid_price.currency == "INR"


def test_excluded_keywords_and_bait_posts(cfg, job_by_id):
    j = job_by_id["upwork:66010"].model_copy(update={"title": "Content Writer Needed Beginners welcome. Apply now"})
    flags = g.prescreen_job(j, cfg)
    assert flags and flags[0].startswith("excluded keyword:")


def test_proposal_contact_details_blocked():
    e = est("j", 150)
    _, errors = g.check_proposal(proposal("j", 150, extra="Email me at bob@mail.com or +1 555 123 4567"), e, [])
    assert any("email" in x for x in errors)
    assert any("phone" in x for x in errors)
    _, errors = g.check_proposal(proposal("j", 150, extra=f"See {PORTFOLIO_URL}"), e, [PORTFOLIO_URL])
    assert errors == []


def test_proposal_price_and_disclosure():
    p = proposal("j", 120).model_copy(update={"ai_disclosure_included": False})
    _, errors = g.check_proposal(p, est("j", 150), [])
    assert any("disclosure" in x for x in errors)
    assert any("must equal" in x for x in errors)


def reply(intent="question", change=0.0, text="Sure, happy to help."):
    return ClientReply(job_id="j", reply_text=text, intent_detected=intent, requirements_update=[], price_change=PriceChange(amount=change))


def test_client_reply_rules(cfg):
    _, ok = g.check_client_reply(reply("negotiation", -50), "can you do it cheaper?", 150, cfg)
    assert not ok  # 33% discount > 10%
    _, ok = g.check_client_reply(reply("scope_change", 0), "add 2 more sites", 150, cfg)
    assert not ok
    r, ok = g.check_client_reply(reply(), "I want a refund", 150, cfg)
    assert ok and r.escalate
    r, ok = g.check_client_reply(reply(), "let's talk on Telegram", 150, cfg)
    assert ok and r.escalate
    _, ok = g.check_client_reply(reply(text="Add me on WhatsApp"), "hi", 150, cfg)
    assert not ok


def plan(tasks, methods=("automated",)):
    return PlanResult(
        project_id="p", spec_md="x",
        acceptance_criteria=[AcceptanceCriterion(id=f"AC{i}", criterion="c", test_method=m) for i, m in enumerate(methods)],
        task_graph=[TaskNode(id=t, description="d", depends_on=deps, builder="code") for t, deps in tasks],
        tech_stack=[], open_questions=[],
    )


def test_plan_checks():
    assert g.check_plan(plan([("T1", []), ("T2", ["T1"])])) == []
    assert "task graph has a cycle" in g.check_plan(plan([("T1", ["T2"]), ("T2", ["T1"])]))
    assert any("unknown task" in e for e in g.check_plan(plan([("T1", ["T9"])])))
    assert any("automated" in e for e in g.check_plan(plan([("T1", [])], methods=("manual", "rubric"))))


def qa(results, issues=()):
    return QAResult(
        project_id="p", verdict="pass",
        criteria_results=[CriterionResult(id=f"AC{i}", result=r, evidence="e") for i, r in enumerate(results)],
        issues=[QAIssue(severity=s, description="d", fix="f") for s in issues],
        scores=QAScores(functionality=90, quality=90, security=90, originality=90), round=1,
    )


def test_qa_verdict_recomputed():
    assert g.enforce_qa(qa(["pass", "fail"]), True).verdict == "fail"
    assert g.enforce_qa(qa(["pass"], issues=["major"]), True).verdict == "fail"
    assert g.enforce_qa(qa(["pass"]), False).verdict == "fail"
    assert g.enforce_qa(qa(["pass"], issues=["minor"]), True).verdict == "pass"


def test_delivery_needs_qa_pass_and_notes():
    d = DeliveryResult(project_id="p", action="deliver", handoff_notes_md="## Setup\n## Run\n## Known limitations", revisions_used=0)
    assert g.check_delivery(d, g.enforce_qa(qa(["pass"]), True), 2) == []
    assert "cannot deliver without a QA pass" in g.check_delivery(d, None, 2)
    leaky = d.model_copy(update={"handoff_notes_md": d.handoff_notes_md + "\napi_key = 'sk-ant-abc123456789xyz'"})
    assert any("secret" in e for e in g.check_delivery(leaky, g.enforce_qa(qa(["pass"]), True), 2))


def test_missing_referenced_files():
    notes = "Run `pip install -r requirements.txt` then `python run.py`; output `products.xlsx`. See `HANDOFF.md`."
    assert g.missing_referenced_files(notes, ["run.py", "products.xlsx"]) == ["requirements.txt"]
    assert g.missing_referenced_files(notes, ["run.py", "products.xlsx", "requirements.txt"]) == []


def test_transitions():
    assert g.can_transition("qa", "delivered")
    assert not g.can_transition("building", "delivered")
    assert g.can_transition("planning", "escalated")
    assert not g.can_transition("rejected", "qualified")
