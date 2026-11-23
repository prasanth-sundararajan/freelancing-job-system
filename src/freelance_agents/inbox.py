"""Client messages: read Freelancer threads for our projects and draft replies.

Drafts go to the approval queue (kind="reply"). Nothing is sent until a human
runs `send-reply <id>`.
"""
from __future__ import annotations

import json

from . import guardrails as g
from .agents import ClientCommsAgent, Ctx
from .schemas import Job


def current_price(ctx: Ctx, job_id: str, job: Job) -> float:
    est = ctx.db.latest_output(job_id, "estimator")
    return float(est["bid_price"]["amount"]) if est else float(job.budget.max or 0)


def process_inbox(ctx: Ctx, api) -> dict:
    """Draft a reply for every project thread whose latest message is from the client."""
    me = api.self_id()
    agent = ClientCommsAgent(ctx)
    stats = {"threads": 0, "not_our_job": 0, "up_to_date": 0, "drafted": 0, "blocked": 0, "escalated": 0}
    for item in api.list_threads():
        stats["threads"] += 1
        thread = item.get("thread") or item
        context = thread.get("context") or {}
        if context.get("type") != "project" or not context.get("id"):
            stats["not_our_job"] += 1
            continue
        job_id = f"freelancer:{context['id']}"
        row = ctx.db.get_job(job_id)
        if row is None:
            stats["not_our_job"] += 1
            continue
        thread_id = item.get("id") or thread.get("id")
        messages = api.list_messages(thread_id)
        if not messages or messages[-1].get("from_user") == me:
            stats["up_to_date"] += 1
            continue
        last = messages[-1]
        if ctx.db.reply_drafted(job_id, last.get("id")):
            stats["up_to_date"] += 1
            continue

        job = Job.model_validate(json.loads(row["data"]))
        history = [{"from": "me" if m.get("from_user") == me else "client", "message": m.get("message", "")}
                   for m in messages[-20:]]
        reply, send_allowed = agent.run(job, history, last.get("message", ""), current_price(ctx, job_id, job))
        ctx.db.save_output(job_id, "client_comms", reply.model_dump())
        ctx.db.enqueue_approval(job_id, "reply", {
            **reply.model_dump(), "thread_id": thread_id, "message_id": last.get("id"),
            "client_message": last.get("message", ""), "send_allowed": send_allowed,
        })
        stats["drafted"] += 1
        stats["blocked"] += 0 if send_allowed else 1
        stats["escalated"] += 1 if reply.escalate else 0
        if row["state"] == "submitted":
            ctx.db.set_state(job_id, "negotiating")
            ctx.db.log("orchestrator", "transition", job_id, "submitted -> negotiating: client replied")
    return stats


def send_reply(ctx: Ctx, api, approval_id: int, override_text: str | None = None) -> str:
    """Human-approved send of one reply draft."""
    row = ctx.db.get_approval(approval_id)
    if row is None or row["kind"] != "reply" or row["status"] != "pending":
        raise ValueError(f"no pending reply draft with id {approval_id}")
    payload = json.loads(row["payload"])
    text = override_text or payload["reply_text"]
    findings = g.contact_findings(text)
    if findings:
        raise g.GuardrailViolation([f"reply contains {f}" for f in findings])
    if not payload.get("send_allowed") and not override_text:
        raise g.GuardrailViolation([f"draft was blocked: {payload.get('escalation_reason')}. Send your own text with --text"])
    if ctx.cfg["kill_switch"]:
        raise g.GuardrailViolation(["kill switch is on"])
    if ctx.cfg["dry_run"]:
        message = "dry run: reply NOT sent (set dry_run: false to send)"
    else:
        api.send_message(payload["thread_id"], text)
        message = "reply sent on Freelancer"
    ctx.db.decide_approval_id(approval_id, "sent" if not ctx.cfg["dry_run"] else "approved_dry_run")
    ctx.db.log("client_comms", "human_approved_reply", row["job_id"], message)
    return message
