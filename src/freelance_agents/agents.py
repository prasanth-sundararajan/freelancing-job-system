"""Agents from skills.md. Each one: build input -> call model -> run code guardrails.

The system prompt for every agent comes from its section of skills.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from . import guardrails as g
from .db import DB
from .llm import LLM
from .schemas import (
    BuilderResult,
    ClientReply,
    DeliveryResult,
    EstimatorResult,
    FinanceReport,
    Job,
    PlanResult,
    ProposalResult,
    QAResult,
    QualifierResult,
    SubmitResult,
)
from .skills_loader import Skills


class DailyLimitReached(Exception):
    pass


@dataclass
class Ctx:
    cfg: dict
    db: DB
    llm: LLM
    skills: Skills
    portfolio: list[dict] = field(default_factory=list)


def as_data(tag: str, obj) -> str:
    """Wrap outside data in tags so the model treats it as data, not instructions.

    sort_keys keeps the text stable, which helps prompt caching.
    """
    if isinstance(obj, BaseModel):
        obj = obj.model_dump()
    return f"<{tag}>\n{json.dumps(obj, indent=2, sort_keys=True, default=str)}\n</{tag}>"


class Agent:
    name = ""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    @property
    def cfg(self) -> dict:
        return self.ctx.cfg

    def ask(self, user: str, schema: type[BaseModel], job_id: str | None = None):
        system = self.ctx.skills.prompt_for(self.name)
        try:
            return self.ctx.llm.structured(agent=self.name, system=system, user=user, schema=schema)
        finally:
            cost = self.ctx.llm.last_cost
            if job_id:
                self.ctx.db.add_cost(job_id, cost)
            self.ctx.db.log(self.name, "llm_call", job_id, cost=cost)


# --- 2. Scout ----------------------------------------------------------------
class ScoutAgent(Agent):
    """Code-only for structured sources (API, file). Model normalization of raw
    text (alert emails, RSS) is a later step."""

    name = "scout"

    def ingest(self, source_name: str, records: list[dict]) -> dict:
        if source_name not in self.cfg["sources"]["allowed"]:
            raise g.GuardrailViolation([f"source '{source_name}' is not in sources.allowed"])
        max_age = timedelta(hours=self.cfg["max_job_age_hours"])
        counts = {"new": 0, "duplicate": 0, "too_old": 0, "invalid": 0}
        for raw in records:
            try:
                job = Job.model_validate(raw)
                posted = datetime.fromisoformat(job.posted_at.replace("Z", "+00:00"))
            except (ValidationError, ValueError) as e:
                counts["invalid"] += 1
                self.ctx.db.log(self.name, "invalid_record", raw.get("job_id"), str(e)[:500])
                continue
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - posted > max_age:
                counts["too_old"] += 1
                continue
            if self.ctx.db.insert_job(job.model_dump()):
                counts["new"] += 1
                self.ctx.db.log(self.name, "job_discovered", job.job_id, source_name)
            else:
                counts["duplicate"] += 1
        return counts


# --- 3. Qualifier ------------------------------------------------------------
class QualifierAgent(Agent):
    name = "qualifier"

    def run(self, job: Job) -> QualifierResult:
        flags = g.prescreen_job(job, self.cfg)
        if flags:  # hard rule: no model call, no cost
            return g.skip_result(job.job_id, flags)
        unknown = g.client_unknown(job)
        note = (
            "\nClient details are not available from this source (all null). Treat them as neutral "
            "(client_quality about 12) and do not list missing client data as a red flag.\n"
            if unknown and self.cfg.get("allow_unknown_payment_status") else ""
        )
        user = (
            "Score this job.\n\n"
            f"Capabilities: {json.dumps(self.cfg['capabilities'])}\n"
            f"Minimum score to pursue: {self.cfg['min_score']}\n{note}\n"
            f"{as_data('job_post', job)}"
        )
        result = self.ask(user, QualifierResult, job.job_id)
        return g.enforce_qualifier(result.model_copy(update={"job_id": job.job_id}), self.cfg, unknown_client=unknown)


# --- 4. Estimator ------------------------------------------------------------
class EstimatorAgent(Agent):
    name = "estimator"

    def run(self, job: Job, qualifier: dict) -> tuple[EstimatorResult, list[str]]:
        b, comp = job.budget, job.competition
        competition = (
            f"Competition: {comp.bid_count} bids so far, average bid {comp.bid_avg:.0f} {b.currency}. "
            "Price competitively: usually at or a little below the average bid, never far above it.\n"
            if comp.bid_avg else ""
        )
        user = (
            "Estimate this job.\n\n"
            f"Currency: bid_price.amount MUST be in {b.currency} (the project's currency), within the client's "
            f"budget of {b.min:.0f}-{b.max:.0f} {b.currency}. 1 {b.currency} = {b.usd_rate} USD. "
            "est_ai_cost_usd values are in USD.\n"
            f"{competition}"
            f"Minimum margin: {self.cfg['min_margin'] * 100:.0f}%\n"
            f"Revision buffer: {self.cfg['budget']['revision_buffer'] * 100:.0f}%\n\n"
            f"{as_data('job_post', job)}\n\n{as_data('qualifier_output', qualifier)}"
        )
        est = self.ask(user, EstimatorResult, job.job_id)
        return g.enforce_estimate(est.model_copy(update={"job_id": job.job_id}), job, self.cfg)


# --- 5. Proposal Writer ------------------------------------------------------
def search_portfolio(job: Job, portfolio: list[dict], k: int = 3) -> list[dict]:
    """Keyword-overlap search. Replace with vector search (pgvector) later."""
    words = set(re.findall(r"[a-z]+", f"{job.title} {' '.join(job.skills)}".lower()))
    scored = []
    for item in portfolio:
        item_words = set(re.findall(r"[a-z]+", f"{item.get('title', '')} {' '.join(item.get('tags', []))}".lower()))
        overlap = len(words & item_words)
        if overlap:
            scored.append((overlap, item))
    return [item for _, item in sorted(scored, key=lambda s: -s[0])[:k]]


class ProposalWriterAgent(Agent):
    name = "proposal_writer"

    def run(self, job: Job, est: EstimatorResult) -> ProposalResult:
        samples = search_portfolio(job, self.ctx.portfolio)
        urls = [s["url"] for s in samples]
        cur = est.bid_price.currency
        user = (
            "Write the proposal.\n\n"
            f"Write the price as {est.bid_price.amount:g} {cur}"
            f"{'' if cur in g.DOLLAR_CURRENCIES else ' (no $ sign)'}.\n\n"
            f"{as_data('job_post', job)}\n\n{as_data('estimate', est)}\n\n"
            f"{as_data('portfolio_samples', samples)}\n\n"
            "Use only the portfolio samples above. If there are none, don't mention a portfolio."
        )
        proposal = self.ask(user, ProposalResult, job.job_id).model_copy(update={"job_id": job.job_id})
        proposal, errors = g.check_proposal(proposal, est, urls)
        if errors:  # one rewrite with the failed checks listed
            retry = (
                f"{user}\n\nYour previous draft failed these checks. Fix all of them:\n"
                + "\n".join(f"- {e}" for e in errors)
                + f"\n\n{as_data('previous_draft', proposal)}"
            )
            proposal = self.ask(retry, ProposalResult, job.job_id).model_copy(update={"job_id": job.job_id})
            proposal, errors = g.check_proposal(proposal, est, urls)
        if errors:
            raise g.GuardrailViolation(errors)
        return proposal


# --- 6. Submitter (plain code) -----------------------------------------------
class SubmitterAgent(Agent):
    name = "submitter"

    def run(self, job: Job, proposal: ProposalResult) -> SubmitResult:
        db = self.ctx.db
        if self.cfg["kill_switch"]:
            raise g.GuardrailViolation(["kill switch is on"])
        if db.has_bid(job.job_id):
            raise g.GuardrailViolation(["job already has a bid"])
        if db.bids_today() >= self.cfg["daily_bid_limit"]:
            raise DailyLimitReached(f"daily bid limit {self.cfg['daily_bid_limit']} reached")

        platform = self.cfg["platforms"].get(job.platform, {"api_bid_allowed": False})
        if not platform.get("api_bid_allowed"):
            db.enqueue_approval(job.job_id, "proposal", proposal.model_dump())
            result = SubmitResult(job_id=job.job_id, channel="human_approval_queue", status="queued")
        elif self.cfg["dry_run"]:
            result = SubmitResult(job_id=job.job_id, channel="api", status="submitted", platform_bid_id="dry-run")
        else:
            # Real submission via the platform's official bidding API goes here.
            raise NotImplementedError(f"live bidding for '{job.platform}' is not implemented; keep dry_run: true")

        db.add_bid(job.job_id, result.channel, result.status, result.platform_bid_id, result.credits_spent)
        db.log(self.name, f"bid_{result.status}", job.job_id, result.channel)
        return result


# --- 7. Client Communication -------------------------------------------------
class ClientCommsAgent(Agent):
    name = "client_comms"

    def run(self, job: Job, thread: list[dict], client_message: str, current_price: float) -> tuple[ClientReply, bool]:
        """Returns (reply, send_allowed). Sending is done by the caller, only if allowed."""
        user = (
            "Draft a reply to the latest client message.\n\n"
            f"Current agreed price: ${current_price}\n"
            f"Max discount: {self.cfg['max_discount_pct']}%\n\n"
            f"{as_data('job_post', job)}\n\n{as_data('thread', thread)}\n\n"
            f"<client_message>\n{client_message}\n</client_message>"
        )
        reply = self.ask(user, ClientReply, job.job_id).model_copy(update={"job_id": job.job_id})
        return g.check_client_reply(reply, client_message, current_price, self.cfg)


# --- 8. Planner --------------------------------------------------------------
class PlannerAgent(Agent):
    name = "planner"

    def run(self, job: Job, contract: dict) -> PlanResult:
        user = f"Plan this project.\n\n{as_data('job_post', job)}\n\n{as_data('contract', contract)}"
        plan = self.ask(user, PlanResult, job.job_id).model_copy(update={"project_id": job.job_id})
        errors = g.check_plan(plan)
        if errors:
            raise g.GuardrailViolation(errors)
        return plan


# --- 9. Builder --------------------------------------------------------------
BUILDER_TOOLS = [
    {"name": "write_file", "description": "Create or overwrite a file in the project folder.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"name": "read_file", "description": "Read a file from the project folder.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_files", "description": "List files in the project folder.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run", "description": "Run ONE command in the project folder: `python ...`, `pytest ...` or "
                                   "`pip install <allowlisted packages>`. No pipes or chaining.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "finish", "description": "Call when all tasks are done and tests pass, or when blocked.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["done", "blocked"]},
         "notes": {"type": "string"},
         "tests_added": {"type": "array", "items": {"type": "string"}},
         "blocked_reason": {"type": "string"}},
         "required": ["status", "notes"]}},
]

_ARTIFACT_TYPES = {".py": "code", ".js": "code", ".html": "code", ".css": "code", ".md": "doc", ".txt": "doc",
                   ".png": "image", ".jpg": "image", ".csv": "data", ".xlsx": "data", ".json": "data"}


class BuilderAgent(Agent):
    """Writes the deliverable inside a Sandbox using tools, then reports via `finish`."""

    name = "builder"

    def run(self, plan: PlanResult, sandbox, budget_usd: float, qa_feedback: list[dict] | None = None) -> BuilderResult:
        from .sandbox import SandboxError

        finished: dict = {}

        def handle(tool: str, args: dict) -> tuple[str, bool]:
            try:
                if tool == "write_file":
                    sandbox.write(args["path"], args["content"])
                    return f"wrote {args['path']}", False
                if tool == "read_file":
                    return sandbox.read(args["path"]), False
                if tool == "list_files":
                    return "\n".join(sandbox.files()) or "(empty)", False
                if tool == "run":
                    code, out = sandbox.run(args["command"])
                    return f"exit code {code}\n{out}", False
                if tool == "finish":
                    finished.update(args)
                    return "finished", True
                return f"error: unknown tool {tool}", False
            except (SandboxError, KeyError, OSError) as e:
                return f"error: {e}", False

        feedback = f"\n\nQA found these problems in the previous round. Fix all of them:\n{as_data('qa_issues', qa_feedback)}" if qa_feedback else ""
        user = (
            "Build the project described below inside the project folder.\n"
            "Rules: write tests with pytest; install packages with `pip install` (allowlisted only, install pytest first); "
            "run the tests until they pass; include a README.md with Setup, Run and Known limitations sections "
            "and a requirements.txt listing every third-party package used; "
            "never hard-code secrets. When done (or blocked), call `finish`.\n\n"
            f"{as_data('plan', plan)}{feedback}"
        )
        system = self.ctx.skills.prompt_for(self.name).replace(
            "Return only the JSON object described in your Response section.",
            "Work through the tools; report with the `finish` tool instead of returning JSON.")
        try:
            self.ctx.llm.tool_loop(agent=self.name, system=system, user=user, tools=BUILDER_TOOLS,
                                   handle=handle, budget_usd=budget_usd)
        finally:
            self.ctx.db.add_cost(plan.project_id, self.ctx.llm.last_cost)
            self.ctx.db.log(self.name, "tool_loop", plan.project_id, cost=self.ctx.llm.last_cost)

        files = sandbox.files()
        secrets = g.scan_secrets(sandbox.all_text())
        status = finished.get("status", "blocked")
        blocked_reason = finished.get("blocked_reason") or ("did not call finish" if not finished else None)
        if secrets:
            status, blocked_reason = "blocked", f"possible secrets in files: {secrets}"
        return BuilderResult(
            project_id=plan.project_id,
            task_ids=[t.id for t in plan.task_graph],
            status=status,
            artifacts=[{"path": f, "type": _ARTIFACT_TYPES.get(Path(f).suffix.lower(), "doc")} for f in files],
            tests_added=finished.get("tests_added", []),
            notes=finished.get("notes", ""),
            blocked_reason=blocked_reason if status == "blocked" else None,
            cost_used_usd=self.ctx.llm.last_cost,
        )


# --- 10. QA ------------------------------------------------------------------
class QAAgent(Agent):
    name = "qa"

    def run(self, plan: PlanResult, test_report: dict, scans_passed: bool, round_no: int) -> QAResult:
        """`test_report` must come from tests QA ran itself, never from the Builder."""
        user = (
            f"Review round {round_no}.\n\n{as_data('plan', plan)}\n\n{as_data('test_report', test_report)}\n\n"
            f"Security and plagiarism scans passed: {scans_passed}"
        )
        qa = self.ask(user, QAResult, plan.project_id).model_copy(update={"project_id": plan.project_id, "round": round_no})
        return g.enforce_qa(qa, scans_passed)


# --- 11. Delivery ------------------------------------------------------------
class DeliveryAgent(Agent):
    name = "delivery"

    def run(self, plan: PlanResult, qa: QAResult | None, client_feedback: str | None, revisions_used: int, allowance: int) -> DeliveryResult:
        user = (
            f"Revisions used so far: {revisions_used} of {allowance}.\n\n{as_data('plan', plan)}\n\n"
            f"{as_data('qa', qa.model_dump() if qa else None)}\n\n"
            f"<client_message>\n{client_feedback or ''}\n</client_message>"
        )
        d = self.ask(user, DeliveryResult, plan.project_id).model_copy(update={"project_id": plan.project_id})
        errors = g.check_delivery(d, qa, allowance)
        if errors:
            raise g.GuardrailViolation(errors)
        return d


# --- 12. Finance -------------------------------------------------------------
class FinanceAgent(Agent):
    name = "finance"

    def run(self, period: str, totals: dict) -> FinanceReport:
        """`totals` are computed in code from the database; the model adds insights only."""
        user = f"Write the report for {period}.\n\n{as_data('totals', totals)}"
        report = self.ask(user, FinanceReport)
        money_fields = {k: totals[k] for k in FinanceReport.model_fields if k in totals}
        report = report.model_copy(update={"period": period, **money_fields})
        for s in report.config_suggestions:  # suggestions only, never applied automatically
            self.ctx.db.add_config_suggestion(s.key, s.current, s.suggested, s.reason)
        return report
