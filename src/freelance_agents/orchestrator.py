"""Orchestrator: moves jobs through the state machine (skills.md section 1).

Routing is plain code; only the agents call a model. Phase 1-2 pipeline:
discovered -> qualified -> estimated -> proposed -> submitted / awaiting_approval
"""
from __future__ import annotations

import json

import anthropic
from pydantic import ValidationError

from . import guardrails as g
from .agents import (
    Ctx,
    DailyLimitReached,
    EstimatorAgent,
    ProposalWriterAgent,
    QualifierAgent,
    SubmitterAgent,
)
from .llm import AgentRefusal
from .schemas import EstimatorResult, Job, OrchestratorDecision, ProposalResult

PIPELINE = ["discovered", "qualified", "estimated", "proposed"]
RECOVERABLE = (g.GuardrailViolation, AgentRefusal, ValidationError, ValueError, anthropic.APIError)


class Orchestrator:
    name = "orchestrator"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.qualifier = QualifierAgent(ctx)
        self.estimator = EstimatorAgent(ctx)
        self.writer = ProposalWriterAgent(ctx)
        self.submitter = SubmitterAgent(ctx)

    # --- transitions ---------------------------------------------------------
    def transition(self, job_id: str, frm: str, to: str, reason: str, next_agent: str | None = None) -> None:
        if not g.can_transition(frm, to):
            raise g.GuardrailViolation([f"illegal transition {frm} -> {to}"])
        decision = OrchestratorDecision(
            job_id=job_id, from_state=frm, to_state=to, next_agent=next_agent,
            reason=reason, escalate=(to == "escalated"),
        )
        self.ctx.db.set_state(job_id, to)
        self.ctx.db.save_output(job_id, self.name, decision.model_dump())
        self.ctx.db.log(self.name, "transition", job_id, f"{frm} -> {to}: {reason}")

    # --- main loop -----------------------------------------------------------
    def tick(self) -> dict:
        """Run every job through as many pipeline steps as possible."""
        db, cfg = self.ctx.db, self.ctx.cfg
        if cfg["kill_switch"]:
            db.log(self.name, "halted", detail="kill switch is on")
            return {"halted": True}
        summary: dict[str, int] = {}
        bidding_open = True
        for state in PIPELINE:
            for row in db.jobs_in_state(state):
                if state == "proposed" and not bidding_open:
                    break
                try:
                    to = self.step(row)
                    summary[to] = summary.get(to, 0) + 1
                except DailyLimitReached as e:
                    db.log(self.name, "bid_limit", row["job_id"], str(e))
                    bidding_open = False
                except RECOVERABLE as e:
                    self.fail(row, e)
                    summary["failed_attempts"] = summary.get("failed_attempts", 0) + 1
        return summary

    def step(self, row) -> str:
        db, cfg = self.ctx.db, self.ctx.cfg
        job = Job.model_validate(json.loads(row["data"]))
        state = row["state"]

        cap = job.budget.max * job.budget.usd_rate * cfg["budget"]["per_job_pct"]  # USD
        if job.budget.max > 0 and row["cost_usd"] > cap:
            self.transition(job.job_id, state, "escalated", f"AI cost ${row['cost_usd']:.2f} over cap ${cap:.2f}")
            return "escalated"

        if state == "discovered":
            q = self.qualifier.run(job)
            db.save_output(job.job_id, "qualifier", q.model_dump())
            to = "qualified" if q.decision == "pursue" else "rejected"
            self.transition(job.job_id, state, to, f"score {q.score}: {q.reason}", "estimator" if to == "qualified" else None)
            return to

        if state == "qualified":
            est, errors = self.estimator.run(job, db.latest_output(job.job_id, "qualifier"))
            db.save_output(job.job_id, "estimator", est.model_dump())
            to = "rejected" if errors else "estimated"
            reason = "; ".join(errors) if errors else f"bid ${est.bid_price.amount}, margin {est.margin_pct}%"
            self.transition(job.job_id, state, to, reason, None if errors else "proposal_writer")
            return to

        if state == "estimated":
            est = EstimatorResult.model_validate(db.latest_output(job.job_id, "estimator"))
            proposal = self.writer.run(job, est)
            db.save_output(job.job_id, "proposal_writer", proposal.model_dump())
            self.transition(job.job_id, state, "proposed", "proposal passed checks", "submitter")
            return "proposed"

        if state == "proposed":
            proposal = ProposalResult.model_validate(db.latest_output(job.job_id, "proposal_writer"))
            result = self.submitter.run(job, proposal)
            db.save_output(job.job_id, "submitter", result.model_dump())
            to = "awaiting_approval" if result.status == "queued" else "submitted"
            self.transition(job.job_id, state, to, f"{result.channel}: {result.status}")
            return to

        raise ValueError(f"no pipeline step for state {state}")

    def fail(self, row, error: Exception) -> None:
        db = self.ctx.db
        job_id = row["job_id"]
        attempts = db.bump_retries(job_id)
        db.log(self.name, "agent_failed", job_id, f"attempt {attempts}: {type(error).__name__}: {error}"[:1000])
        if attempts > self.ctx.cfg["max_retries"]:
            self.transition(job_id, row["state"], "escalated", f"failed {attempts} times: {error}"[:500])

    # --- human actions -------------------------------------------------------
    def approve(self, job_id: str, submit: bool = False, api=None) -> str:
        """Approve a queued proposal.

        submit=False: you already submitted it on the platform yourself.
        submit=True:  post this ONE bid via the Freelancer API (needs
                      freelancer.bid_via_api: true). Returns a status message.
        """
        db, cfg = self.ctx.db, self.ctx.cfg
        row = db.get_job(job_id)
        if row is None or row["state"] != "awaiting_approval":
            raise ValueError(f"{job_id} is not awaiting approval")

        bid_id = None
        message = "marked as submitted by you"
        if submit:
            job = Job.model_validate(json.loads(row["data"]))
            if job.platform != "freelancer":
                raise ValueError("--submit only works for Freelancer.com jobs; submit others on the platform yourself")
            if not cfg.get("freelancer", {}).get("bid_via_api"):
                raise ValueError("freelancer.bid_via_api is false in config.yaml; submit on freelancer.com yourself")
            if cfg["kill_switch"]:
                raise g.GuardrailViolation(["kill switch is on"])
            # Use the text you approved (including Telegram edits), not the first draft.
            pending = [a for a in db.pending_approvals("proposal") if a["job_id"] == job_id]
            source = json.loads(pending[-1]["payload"]) if pending else db.latest_output(job_id, "proposal_writer")
            proposal = ProposalResult.model_validate(source)
            if api is not None and api.my_bids(int(job_id.split(":", 1)[1])):
                raise ValueError("you already have a bid on this project")
            if cfg["dry_run"]:
                bid_id, message = "dry-run", "dry run: bid NOT sent (set dry_run: false to send)"
            else:
                if api is None:
                    raise ValueError("no Freelancer API client")
                result = api.place_bid(
                    project_id=int(job_id.split(":", 1)[1]),
                    bidder_id=api.self_id(),
                    amount=proposal.bid_price.amount,
                    period=proposal.timeline_days,
                    description=proposal.proposal_text,
                )
                if not api.my_bids(int(job_id.split(":", 1)[1])):
                    raise ValueError("Freelancer accepted the request but the bid isn't showing on the project")
                bid_id, message = str(result.get("id")), f"bid placed on Freelancer (bid id {result.get('id')})"
            db.log(self.name, "human_approved_api_bid", job_id, message)

        db.decide_approval(job_id, "approved")
        db.set_bid_status(job_id, "submitted", bid_id)
        self.transition(job_id, "awaiting_approval", "submitted", f"human approved: {message}")
        return message

    def reject(self, job_id: str) -> None:
        row = self.ctx.db.get_job(job_id)
        if row is None or row["state"] != "awaiting_approval":
            raise ValueError(f"{job_id} is not awaiting approval")
        self.ctx.db.decide_approval(job_id, "rejected")
        self.ctx.db.set_bid_status(job_id, "rejected")
        self.transition(job_id, "awaiting_approval", "rejected", "human rejected the proposal")
