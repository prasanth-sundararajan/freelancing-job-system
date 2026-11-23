"""Delivery pipeline for a won job: plan -> build -> QA (up to 3 rounds) -> package.

    start-project <job_id> --notes "what was agreed"   # you won the job
    build <job_id>                                     # runs this pipeline

The finished zip and handoff notes go to the approval queue (kind="delivery").
You upload them to the client yourself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import guardrails as g
from .agents import BuilderAgent, Ctx, DeliveryAgent, PlannerAgent, QAAgent
from .config import ROOT
from .llm import BudgetExceeded
from .sandbox import Sandbox
from .schemas import Job, PlanResult, QAResult

MAX_QA_ROUNDS = 3


def workspace_for(job_id: str) -> Path:
    return ROOT / "data" / "projects" / re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)


class DeliveryPipeline:
    name = "orchestrator"

    def __init__(self, ctx: Ctx, sandbox_factory=None):
        self.ctx = ctx
        self.planner, self.builder, self.qa, self.delivery = PlannerAgent(ctx), BuilderAgent(ctx), QAAgent(ctx), DeliveryAgent(ctx)
        sb_cfg = ctx.cfg.get("sandbox", {})
        self.sandbox_factory = sandbox_factory or (lambda job_id: Sandbox(
            workspace_for(job_id), sb_cfg.get("allowed_packages", []), sb_cfg.get("timeout_seconds", 180)))

    # --- helpers ---------------------------------------------------------------
    def _job(self, job_id: str):
        row = self.ctx.db.get_job(job_id)
        if row is None:
            raise ValueError(f"unknown job {job_id}")
        return row, Job.model_validate(json.loads(row["data"]))

    def _move(self, job_id: str, frm: str, to: str, reason: str) -> None:
        if not g.can_transition(frm, to):
            raise g.GuardrailViolation([f"illegal transition {frm} -> {to}"])
        self.ctx.db.set_state(job_id, to)
        self.ctx.db.log(self.name, "transition", job_id, f"{frm} -> {to}: {reason}")

    def _budget(self, job: Job, job_id: str) -> float:
        """Remaining AI budget in USD: 80% of the per-job cap minus what's already spent."""
        est = self.ctx.db.latest_output(job_id, "estimator") or {}
        price = (est.get("bid_price") or {}).get("amount") or job.budget.max
        cap = price * job.budget.usd_rate * self.ctx.cfg["budget"]["per_job_pct"] * 0.8
        return max(0.0, cap - self.ctx.db.get_job(job_id)["cost_usd"])

    # --- steps -----------------------------------------------------------------
    def start(self, job_id: str, notes: str = "", revision_allowance: int = 2) -> None:
        """You won the job: record the agreed scope and move to planning."""
        row, job = self._job(job_id)
        if row["state"] not in ("submitted", "negotiating"):
            raise ValueError(f"{job_id} is '{row['state']}'; only submitted or negotiating jobs can be started")
        est = self.ctx.db.latest_output(job_id, "estimator") or {}
        requirements = []
        for o in self.ctx.db.outputs(job_id):
            if o["agent"] == "client_comms":
                requirements += json.loads(o["output"]).get("requirements_update", [])
        contract = {"price": est.get("bid_price"), "timeline_days": est.get("timeline_days"),
                    "revision_allowance": revision_allowance, "agreed_notes": notes, "requirements": requirements}
        self.ctx.db.save_output(job_id, "contract", contract)
        self._move(job_id, row["state"], "won", "you marked the job as won")
        self._move(job_id, "won", "planning", "contract recorded")

    def run(self, job_id: str) -> str:
        row, job = self._job(job_id)
        db = self.ctx.db
        if self.ctx.cfg["kill_switch"]:
            return "kill switch is on"

        if row["state"] == "planning":
            contract = db.latest_output(job_id, "contract") or {}
            plan = self.planner.run(job, contract)
            db.save_output(job_id, "planner", plan.model_dump())
            if plan.open_questions:
                db.enqueue_approval(job_id, "questions", {"questions": plan.open_questions})
                return f"waiting for answers to {len(plan.open_questions)} question(s); see `queue`"
            self._move(job_id, "planning", "building", "plan ready")
            row = db.get_job(job_id)

        if row["state"] not in ("building", "qa"):
            return f"{job_id} is '{row['state']}'; nothing to build"

        plan = PlanResult.model_validate(db.latest_output(job_id, "planner"))
        sandbox = self.sandbox_factory(job_id)
        feedback = None
        qa: QAResult | None = None
        for round_no in range(1, MAX_QA_ROUNDS + 1):
            if db.get_job(job_id)["state"] == "qa":
                self._move(job_id, "qa", "building", f"QA round {round_no - 1} failed")
            try:
                built = self.builder.run(plan, sandbox, self._budget(job, job_id), feedback)
            except BudgetExceeded as e:
                self._move(job_id, "building", "escalated", f"AI budget used up: {e}")
                return "escalated: budget used up"
            db.save_output(job_id, "builder", built.model_dump())
            if built.status == "blocked":
                self._move(job_id, "building", "escalated", f"builder blocked: {built.blocked_reason}")
                return f"escalated: {built.blocked_reason}"
            self._move(job_id, "building", "qa", "builder finished")

            # QA runs the tests itself and never trusts the Builder's report.
            sandbox.run("pip install pytest")
            code, output = sandbox.run("pytest -q")
            secrets = g.scan_secrets(sandbox.all_text())
            report = {"pytest_exit_code": code, "pytest_output": output, "files": sandbox.files(),
                      "secret_scan": secrets or "clean"}
            qa = self.qa.run(plan, report, scans_passed=(code == 0 and not secrets), round_no=round_no)
            db.save_output(job_id, "qa", qa.model_dump())
            if qa.verdict == "pass":
                break
            feedback = [i.model_dump() for i in qa.issues] or [{"description": "tests failed", "fix": output[-1500:]}]
        else:
            self._move(job_id, "qa", "escalated", f"QA failed {MAX_QA_ROUNDS} rounds")
            return f"escalated: QA failed {MAX_QA_ROUNDS} rounds"

        contract = db.latest_output(job_id, "contract") or {}
        result = self.delivery.run(plan, qa, None, 0, contract.get("revision_allowance", 2))
        db.save_output(job_id, "delivery", result.model_dump())
        zip_path = sandbox.package(ROOT / "data" / "deliveries" / f"{workspace_for(job_id).name}.zip",
                                   {"HANDOFF.md": result.handoff_notes_md})
        missing = g.missing_referenced_files(result.handoff_notes_md, sandbox.files())
        db.enqueue_approval(job_id, "delivery", {"zip": str(zip_path), "handoff_notes_md": result.handoff_notes_md,
                                                 "check_before_sending": [f"handoff mentions missing file: {m}" for m in missing]})
        self._move(job_id, "qa", "delivered", f"QA passed; package {zip_path.name}")
        return f"delivered: {zip_path}"
