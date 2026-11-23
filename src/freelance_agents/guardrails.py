"""Hard guardrails: the "Enforced in code" rules from skills.md.

Every function here is plain code that runs on the model's output, whatever
the model returned. The model cannot talk its way past these checks.
"""
from __future__ import annotations

import re

from .schemas import (
    ClientReply,
    DeliveryResult,
    EstimatorResult,
    Job,
    PlanResult,
    ProposalResult,
    QAResult,
    QualifierBreakdown,
    QualifierResult,
)


class GuardrailViolation(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --- Patterns ----------------------------------------------------------------
MESSAGING = re.compile(r"\b(whats\s?app|telegram|skype|signal|wechat|discord)\b", re.I)
OFF_PLATFORM = re.compile(
    r"\b(whats\s?app|telegram|skype|wechat|discord|pay(?:ment)? (?:me )?outside|outside (?:the )?(?:platform|site|upwork|freelancer|fiverr)"
    r"|pay (?:you )?directly|direct payment|paypal\.me|crypto(?:currency)?)\b",
    re.I,
)
UNPAID_SAMPLE = re.compile(r"\b(free (?:sample|trial|test)|test task|unpaid (?:test|trial|sample)|sample for free)\b", re.I)
ACADEMIC = re.compile(
    r"\b(my (?:assignment|homework|exam|thesis|dissertation|coursework)|take my (?:exam|class|test|quiz)|do my (?:homework|assignment))\b",
    re.I,
)
ESCALATION_TRIGGERS = re.compile(r"\b(refund|dispute|chargeback|lawyer|legal|scam|sue|report you)\b", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
URL = re.compile(r"https?://[^\s)\"'<>]+", re.I)
AI_DISCLOSURE = re.compile(r"\bAI\b|AI-assisted|artificial intelligence", re.I)
SECRET = re.compile(
    r"(sk-ant-[\w-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}"
    r"|(?:password|passwd|secret|api_key|apikey|token)\s*[:=]\s*['\"][^'\"]{6,}['\"])",
    re.I,
)

# --- Orchestrator: allowed state transitions ---------------------------------
TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"qualified", "rejected"},
    "qualified": {"estimated", "rejected"},
    "estimated": {"proposed", "rejected"},
    "proposed": {"awaiting_approval", "submitted"},
    "awaiting_approval": {"submitted", "rejected"},
    "submitted": {"negotiating", "won", "lost"},
    "negotiating": {"won", "lost"},
    "won": {"planning"},
    "planning": {"building"},
    "building": {"qa"},
    "qa": {"building", "delivered"},  # delivered only reachable from qa (after a pass)
    "delivered": {"revision", "closed"},
    "revision": {"building", "closed"},
}
TERMINAL = {"rejected", "lost", "closed", "escalated"}


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state in TERMINAL:
        return False
    if to_state == "escalated":
        return True
    return to_state in TRANSITIONS.get(from_state, set())


# --- Shared scanners ---------------------------------------------------------
def _norm_url(url: str) -> str:
    return url.rstrip("/.,;:")


def contact_findings(text: str, allowed_urls: list[str] | tuple = ()) -> list[str]:
    """Find contact details or off-platform links in outbound text."""
    found: list[str] = []
    if EMAIL.search(text):
        found.append("email address")
    for m in PHONE.finditer(text):
        if sum(c.isdigit() for c in m.group()) >= 9:
            found.append("phone number")
            break
    if MESSAGING.search(text):
        found.append("messaging app mention")
    allowed = {_norm_url(u) for u in allowed_urls}
    for url in URL.findall(text):
        if _norm_url(url) not in allowed:
            found.append(f"external link {url}")
    return found


def scan_secrets(text: str) -> list[str]:
    return [m.group(0)[:12] + "..." for m in SECRET.finditer(text)]


# --- Qualifier ---------------------------------------------------------------
def exclude_pattern(keywords: list[str]) -> re.Pattern | None:
    if not keywords:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.I)


def prescreen_job(job: Job, cfg: dict) -> list[str]:
    """Keyword checks that force `skip` before any model call."""
    text = f"{job.title}\n{job.description}"
    flags: list[str] = []
    excluded = exclude_pattern(cfg.get("exclude_keywords") or [])
    if excluded and (m := excluded.search(text)):
        flags.append(f"excluded keyword: {m.group(0).lower()}")
    if OFF_PLATFORM.search(text):
        flags.append("off-platform contact or payment")
    if UNPAID_SAMPLE.search(text):
        flags.append("unpaid test work")
    if ACADEMIC.search(text):
        flags.append("academic cheating")
    verified = job.client.payment_verified
    if verified is False and not cfg.get("allow_unverified_payment"):
        flags.append("client payment not verified")
    if verified is None and not cfg.get("allow_unknown_payment_status"):
        flags.append("client payment status unknown")
    return flags


def skip_result(job_id: str, flags: list[str]) -> QualifierResult:
    return QualifierResult(
        job_id=job_id,
        score=0,
        breakdown=QualifierBreakdown(skill_match=0, ai_deliverable=0, budget_vs_effort=0, client_quality=0, risk_penalty=100),
        red_flags=flags,
        decision="skip",
        reason="Rejected by code guardrail before model call: " + ", ".join(flags),
    )


NEUTRAL_CLIENT_QUALITY = 12
# Red flags that only say "we don't know who the client is".
_CLIENT_WORDS = r"\b(payment|client|rating|hires?|spend|history|record|reputation)\b"
_MISSING_WORDS = r"\b(unverified|not verified|unknown|missing|no|empty|unavailable|blank)\b"
MISSING_CLIENT_FLAG = re.compile(f"{_CLIENT_WORDS}.*{_MISSING_WORDS}|{_MISSING_WORDS}.*{_CLIENT_WORDS}", re.I)


def client_unknown(job: Job) -> bool:
    c = job.client
    return c.payment_verified is None and c.rating is None and c.hires is None and c.spent is None


def enforce_qualifier(result: QualifierResult, cfg: dict, unknown_client: bool = False) -> QualifierResult:
    """Recompute score and decision in code; the model can't override them."""
    b = result.breakdown
    clamp = lambda v, hi: max(0, min(hi, v))  # noqa: E731
    flags = list(result.red_flags)
    client_quality = clamp(b.client_quality, 25)
    if unknown_client and cfg.get("allow_unknown_payment_status"):
        client_quality = NEUTRAL_CLIENT_QUALITY
        flags = [f for f in flags if not MISSING_CLIENT_FLAG.search(f)]
    result = result.model_copy(update={"red_flags": flags})
    breakdown = QualifierBreakdown(
        skill_match=clamp(b.skill_match, 25),
        ai_deliverable=clamp(b.ai_deliverable, 25),
        budget_vs_effort=clamp(b.budget_vs_effort, 25),
        client_quality=client_quality,
        risk_penalty=clamp(b.risk_penalty, 100),
    )
    score = max(
        0,
        breakdown.skill_match + breakdown.ai_deliverable + breakdown.budget_vs_effort
        + breakdown.client_quality - breakdown.risk_penalty,
    )
    decision = "pursue" if score >= cfg["min_score"] and not result.red_flags else "skip"
    reason = result.reason
    if decision != result.decision:
        reason += f" [guardrail: decision changed to '{decision}' (score {score}, red flags {len(result.red_flags)})]"
    return result.model_copy(update={"breakdown": breakdown, "score": score, "decision": decision, "reason": reason})


# --- Estimator ---------------------------------------------------------------
DOLLAR_CURRENCIES = {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}


def enforce_estimate(est: EstimatorResult, job: Job, cfg: dict) -> tuple[EstimatorResult, list[str]]:
    """Recompute cost and margin in code. Returns the fixed estimate and reasons to reject.

    The bid is in the project's currency; AI cost is in USD. Margin is computed in USD.
    """
    buffer = cfg["budget"]["revision_buffer"]
    total = round(sum(t.est_ai_cost_usd for t in est.tasks) * (1 + buffer), 2)
    cur = job.budget.currency
    price = est.bid_price.amount
    price_usd = price * job.budget.usd_rate
    margin = round((price_usd - total) / price_usd * 100, 1) if price_usd > 0 else 0.0
    errors: list[str] = []
    if price <= 0:
        errors.append("bid price must be positive")
    if margin < cfg["min_margin"] * 100:
        errors.append(f"margin {margin}% is below minimum {cfg['min_margin'] * 100:.0f}%")
    if job.budget.max > 0 and price > job.budget.max * 1.2:
        errors.append(f"bid {price} {cur} is more than 20% above client max {job.budget.max} {cur}")
    if job.budget.min > 0 and price < job.budget.min * 0.5:
        errors.append(f"bid {price} {cur} is far below client min {job.budget.min} {cur} (wrong currency?)")
    if price_usd > 0 and total > price_usd * cfg["budget"]["per_job_pct"]:
        errors.append(f"AI cost ${total} exceeds {cfg['budget']['per_job_pct'] * 100:.0f}% of price (${price_usd:.2f})")
    bid_price = est.bid_price.model_copy(update={"currency": cur, "type": job.budget.type})
    return est.model_copy(update={"total_ai_cost_usd": total, "margin_pct": margin, "bid_price": bid_price}), errors


# --- Proposal Writer ---------------------------------------------------------
def check_proposal(p: ProposalResult, est: EstimatorResult, portfolio_urls: list[str]) -> tuple[ProposalResult, list[str]]:
    allowed = {_norm_url(u) for u in portfolio_urls}
    links = [link for link in p.portfolio_links if _norm_url(link) in allowed]
    text = p.proposal_text
    errors: list[str] = []
    words = len(text.split())
    if not 120 <= words <= 250:
        errors.append(f"proposal is {words} words; must be 120-250")
    if not p.ai_disclosure_included or not AI_DISCLOSURE.search(text):
        errors.append("proposal must include an AI-assisted disclosure")
    if abs(p.bid_price.amount - est.bid_price.amount) > 0.01:
        errors.append(f"bid price {p.bid_price.amount} must equal the estimate {est.bid_price.amount}")
    if est.bid_price.currency not in DOLLAR_CURRENCIES and "$" in text:
        errors.append(f"price must be written in {est.bid_price.currency}, not with a $ sign")
    p = p.model_copy(update={"bid_price": p.bid_price.model_copy(update={"currency": est.bid_price.currency})})
    for finding in contact_findings(text, portfolio_urls):
        errors.append(f"proposal contains {finding}")
    if len(p.clarifying_questions) > 2:
        errors.append("at most 2 clarifying questions")
    return p.model_copy(update={"portfolio_links": links}), errors


# --- Client Communication ----------------------------------------------------
def check_client_reply(reply: ClientReply, client_message: str, current_price: float, cfg: dict) -> tuple[ClientReply, bool]:
    """Returns (reply, send_allowed). Escalation reasons are merged into the reply."""
    block: list[str] = []      # reply must not be sent
    escalate: list[str] = []   # reply may be sent, but a human must look

    for finding in contact_findings(reply.reply_text):
        block.append(f"reply contains {finding}")
    change = reply.price_change.amount
    if change < 0 and abs(change) > current_price * cfg["max_discount_pct"] / 100:
        block.append(f"discount ${abs(change)} exceeds {cfg['max_discount_pct']}%")
    if reply.intent_detected == "scope_change" and change <= 0:
        block.append("scope change without a price change")
    if ESCALATION_TRIGGERS.search(client_message):
        escalate.append("client message mentions refund/dispute/legal")
    if OFF_PLATFORM.search(client_message):
        escalate.append("client asked to move off-platform")
    if current_price + max(change, 0) > cfg["approval_threshold"] and reply.intent_detected == "acceptance":
        escalate.append(f"contract above ${cfg['approval_threshold']} needs human approval")
        block.append("acceptance above approval threshold")

    reasons = block + escalate
    if reply.escalate and reply.escalation_reason:
        reasons.insert(0, reply.escalation_reason)
    if reasons:
        reply = reply.model_copy(update={"escalate": True, "escalation_reason": "; ".join(dict.fromkeys(reasons))})
    return reply, not block


# --- Planner -----------------------------------------------------------------
def check_plan(plan: PlanResult) -> list[str]:
    errors: list[str] = []
    acs = plan.acceptance_criteria
    if not acs:
        errors.append("plan needs at least one acceptance criterion")
    elif sum(ac.test_method == "automated" for ac in acs) * 2 < len(acs):
        errors.append("at least half of the acceptance criteria must use automated tests")

    ids = {t.id for t in plan.task_graph}
    for t in plan.task_graph:
        for dep in t.depends_on:
            if dep not in ids:
                errors.append(f"task {t.id} depends on unknown task {dep}")
    graph = {t.id: [d for d in t.depends_on if d in ids] for t in plan.task_graph}
    visiting, done = set(), set()

    def has_cycle(node: str) -> bool:
        if node in done:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        cyclic = any(has_cycle(d) for d in graph[node])
        visiting.discard(node)
        done.add(node)
        return cyclic

    if any(has_cycle(n) for n in graph):
        errors.append("task graph has a cycle")
    return errors


# --- QA ----------------------------------------------------------------------
def enforce_qa(qa: QAResult, scans_passed: bool) -> QAResult:
    passed = (
        scans_passed
        and bool(qa.criteria_results)
        and all(c.result == "pass" for c in qa.criteria_results)
        and not any(i.severity in ("critical", "major") for i in qa.issues)
    )
    return qa.model_copy(update={"verdict": "pass" if passed else "fail"})


# --- Delivery ----------------------------------------------------------------
CODE_SPAN = re.compile(r"`([^`]+)`")
FILE_NAME = re.compile(r"(?<![\w/\\.-])([\w-]+(?:/[\w.-]+)*\.(?:py|txt|md|html|csv|xlsx|json|toml|cfg|ini|yml|yaml|js|css))\b")


def missing_referenced_files(notes: str, files: list[str]) -> list[str]:
    """Files the handoff notes mention inside `code spans` that aren't in the package."""
    names = set(files) | {f.split("/")[-1] for f in files} | {"HANDOFF.md"}
    found = {name for span in CODE_SPAN.findall(notes) for name in FILE_NAME.findall(span)}
    return sorted(m for m in found if m not in names and m.split("/")[-1] not in names)


def check_delivery(d: DeliveryResult, qa: QAResult | None, revision_allowance: int) -> list[str]:
    errors: list[str] = []
    if d.action == "deliver":
        if qa is None or qa.verdict != "pass":
            errors.append("cannot deliver without a QA pass")
        notes = d.handoff_notes_md.lower()
        for heading in ("setup", "run", "known limitations"):
            if heading not in notes:
                errors.append(f"handoff notes missing '{heading}' section")
        for secret in scan_secrets(d.handoff_notes_md):
            errors.append(f"possible secret in handoff notes: {secret}")
    if d.revisions_used > revision_allowance:
        errors.append(f"revisions used {d.revisions_used} exceeds allowance {revision_allowance}")
    return errors
