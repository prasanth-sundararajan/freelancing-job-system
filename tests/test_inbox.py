import json

import pytest

from conftest import est
from freelance_agents import guardrails as g
from freelance_agents.agents import ScoutAgent
from freelance_agents.inbox import process_inbox, send_reply
from freelance_agents.schemas import ClientReply, PriceChange

ME, CLIENT = 555, 777


class FakeAPI:
    def __init__(self, threads, messages):
        self.threads, self.messages, self.sent = threads, messages, []

    def self_id(self):
        return ME

    def list_threads(self, limit=50):
        return self.threads

    def list_messages(self, thread_id, limit=50):
        return self.messages[thread_id]

    def send_message(self, thread_id, text):
        self.sent.append((thread_id, text))
        return {"id": 1}


def reply(jid, text="Sure, happy to add that for 70 more.", intent="scope_change", change=70.0):
    return ClientReply(job_id=jid, reply_text=text, intent_detected=intent, requirements_update=["add 2 sites"],
                       price_change=PriceChange(amount=change))


def setup_ctx(make_ctx, sample_jobs, responses, **overrides):
    ctx = make_ctx(responses, **overrides)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])  # freelancer:88231
    ctx.db.set_state("freelancer:88231", "submitted")
    ctx.db.save_output("freelancer:88231", "estimator", est("freelancer:88231", 150).model_dump())
    return ctx


def api_with(last_from):
    threads = [
        {"id": 1, "thread": {"context": {"type": "project", "id": 88231}}},
        {"id": 2, "thread": {"context": {"type": "project", "id": 99999}}},   # not our job
    ]
    messages = {1: [{"id": 10, "from_user": ME, "message": "Hi, here's my proposal", "time_created": 1},
                    {"id": 11, "from_user": last_from, "message": "Can you add 2 more sites?", "time_created": 2}],
                2: []}
    return FakeAPI(threads, messages)


def test_drafts_reply_for_new_client_message(make_ctx, sample_jobs):
    ctx = setup_ctx(make_ctx, sample_jobs, {"client_comms": lambda jid: reply(jid)})
    stats = process_inbox(ctx, api_with(CLIENT))
    assert stats["drafted"] == 1 and stats["not_our_job"] == 1
    drafts = ctx.db.pending_approvals("reply")
    payload = json.loads(drafts[0]["payload"])
    assert payload["thread_id"] == 1 and payload["message_id"] == 11 and payload["send_allowed"] is True
    assert ctx.db.get_job("freelancer:88231")["state"] == "negotiating"
    # second pass: already drafted, no new model call
    calls = len(ctx.llm.calls)
    assert process_inbox(ctx, api_with(CLIENT))["drafted"] == 0
    assert len(ctx.llm.calls) == calls


def test_no_draft_when_we_replied_last(make_ctx, sample_jobs):
    ctx = setup_ctx(make_ctx, sample_jobs, {})
    assert process_inbox(ctx, api_with(ME))["up_to_date"] == 1


def test_send_reply_dry_run_and_live(make_ctx, sample_jobs):
    ctx = setup_ctx(make_ctx, sample_jobs, {"client_comms": lambda jid: reply(jid)})
    api = api_with(CLIENT)
    process_inbox(ctx, api)
    draft_id = ctx.db.pending_approvals("reply")[0]["id"]
    assert "NOT sent" in send_reply(ctx, api, draft_id)
    assert api.sent == []

    ctx2 = setup_ctx(make_ctx, sample_jobs, {"client_comms": lambda jid: reply(jid)}, dry_run=False)
    api2 = api_with(CLIENT)
    process_inbox(ctx2, api2)
    send_reply(ctx2, api2, ctx2.db.pending_approvals("reply")[0]["id"])
    assert api2.sent == [(1, "Sure, happy to add that for 70 more.")]


def test_blocked_draft_needs_your_own_text(make_ctx, sample_jobs):
    responses = {"client_comms": lambda jid: reply(jid, text="Add me on WhatsApp", intent="question", change=0)}
    ctx = setup_ctx(make_ctx, sample_jobs, responses, dry_run=False)
    api = api_with(CLIENT)
    process_inbox(ctx, api)
    draft_id = ctx.db.pending_approvals("reply")[0]["id"]
    with pytest.raises(g.GuardrailViolation):
        send_reply(ctx, api, draft_id)
    with pytest.raises(g.GuardrailViolation):
        send_reply(ctx, api, draft_id, "email me at bob@mail.com")
    send_reply(ctx, api, draft_id, "Happy to keep everything here on Freelancer.")
    assert api.sent == [(1, "Happy to keep everything here on Freelancer.")]
