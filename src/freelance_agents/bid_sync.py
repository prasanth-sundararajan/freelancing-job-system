"""Keep our records in step with Freelancer.

- A queued proposal you bid on yourself (on the website) -> recorded as submitted.
- A submitted bid the client awarded -> reported so you can start the build.
"""
from __future__ import annotations

from .agents import Ctx
from .orchestrator import Orchestrator

AWARDED = {"awarded", "pending"}  # Freelancer: "pending" = awarded, waiting for you to accept


def sync_bids(ctx: Ctx, api) -> dict:
    orch = Orchestrator(ctx)
    out = {"recorded": [], "awarded": [], "checked": 0}
    for row in ctx.db.all_jobs():
        job_id, state = row["job_id"], row["state"]
        if not job_id.startswith("freelancer:") or state not in ("awaiting_approval", "submitted", "negotiating"):
            continue
        out["checked"] += 1
        bids = api.my_bids(int(job_id.split(":", 1)[1]))
        if not bids:
            continue
        bid = bids[0]
        if state == "awaiting_approval":
            orch.approve(job_id)
            ctx.db.set_bid_status(job_id, "submitted", str(bid.get("id")))
            out["recorded"].append(job_id)
        # Un-awarded bids have award_status None and frontend_bid_status "active".
        status = (bid.get("award_status") or "").lower()
        if status not in AWARDED and (bid.get("frontend_bid_status") or "").lower() in AWARDED:
            status = bid["frontend_bid_status"].lower()
        if status in AWARDED and ctx.db.kv_get(f"award_seen:{job_id}") is None:
            ctx.db.kv_set(f"award_seen:{job_id}", status)
            out["awarded"].append({"job_id": job_id, "award_status": status, "amount": bid.get("amount")})
    return out
