import json

import pytest

from conftest import PORTFOLIO_URL, est, proposal, qual
from freelance_agents.agents import ScoutAgent
from freelance_agents.orchestrator import Orchestrator
from freelance_agents.telegram_bot import ApprovalBot

OWNER, STRANGER = 1001, 666
PRICES = {"freelancer:88231": 150, "upwork:66010": 400}


class FakeTG:
    def __init__(self):
        self.sent, self.answers, self.docs, self.updates = [], [], [], []

    def get_updates(self, offset, timeout=30):
        out, self.updates = self.updates, []
        return out

    def send(self, chat_id, text, buttons=None, html=False):
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "html": html})
        return {"message_id": len(self.sent)}

    def send_document(self, chat_id, path, caption=""):
        self.docs.append(path)

    def answer(self, cid, text=""):
        self.answers.append(text)

    def clear_buttons(self, chat_id, message_id):
        pass


def button_data(msg):
    return [d for row in msg["buttons"] or [] for _, d in row]


FL_TITLE, UP_TITLE = "Python script to scrape product prices", "Simple landing page for a yoga studio"


def card(tg, title):
    return next(m for m in tg.sent if m["text"].startswith("🆕 <b>Proposal") and title in m["text"])


def press(bot, data, user=OWNER):
    bot.handle_update({"update_id": 1, "callback_query": {"id": "cb", "from": {"id": user}, "data": data,
                                                          "message": {"chat": {"id": user}, "message_id": 5}}})


def say(bot, text, user=OWNER):
    bot.handle_update({"update_id": 1, "message": {"from": {"id": user}, "text": text}})


@pytest.fixture
def setup(make_ctx, sample_jobs):
    responses = {
        "qualifier": lambda jid: qual(jid),
        "estimator": lambda jid: est(jid, PRICES[jid]),
        "proposal_writer": lambda jid: proposal(jid, PRICES[jid], links=[PORTFOLIO_URL]),
    }
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs)
    Orchestrator(ctx).tick()
    tg = FakeTG()
    bot = ApprovalBot(ctx, tg, OWNER, api_factory=lambda: None, background_builds=False)
    return ctx, tg, bot


def test_pairing_with_code(make_ctx):
    ctx = make_ctx({})
    tg, paired = FakeTG(), []
    bot = ApprovalBot(ctx, tg, None, api_factory=lambda: None, on_pair=paired.append)
    say(bot, "/start wrong", user=STRANGER)
    assert bot.owner_id is None and tg.sent == []
    say(bot, f"/start {bot.pair_code}", user=OWNER)
    assert bot.owner_id == OWNER and paired == [OWNER]


def test_strangers_are_ignored(setup):
    ctx, tg, bot = setup
    press(bot, "px:1", user=STRANGER)
    assert tg.answers == ["Not allowed"]
    assert len(ctx.db.pending_approvals("proposal")) == 2


def test_proposal_cards_sent_once(setup):
    ctx, tg, bot = setup
    bot.notify_pending()
    cards = [m for m in tg.sent if m["text"].startswith("🆕 <b>Proposal")]
    assert len(cards) == 2 and all(m["chat"] == OWNER and m["html"] for m in cards)
    bot.notify_pending()
    assert len([m for m in tg.sent if m["text"].startswith("🆕 <b>Proposal")]) == 2


def test_card_has_phone_link_and_tap_to_copy_fields(setup):
    ctx, tg, bot = setup
    bot.notify_pending()
    c = card(tg, FL_TITLE)
    assert "Bid amount: <code>150</code> USD" in c["text"] and "Delivered in: <code>2</code> days" in c["text"]
    assert "<pre>" in c["text"] and "AI-assisted" in c["text"]
    assert button_data(c)[0] == "url:https://www.freelancer.com/projects/88231"
    assert not any(d.startswith("pbc:") for d in button_data(c))   # bid_via_api off in tests


def test_card_escapes_html(setup):
    ctx, tg, bot = setup
    a = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    payload = json.loads(a["payload"])
    payload["proposal_text"] += " <script>&</script>"
    ctx.db.update_approval_payload(a["id"], payload)
    bot.notify_pending()
    assert "&lt;script&gt;&amp;&lt;/script&gt;" in card(tg, FL_TITLE)["text"]


def test_approve_submit_won_flow(setup, monkeypatch):
    ctx, tg, bot = setup
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    press(bot, f"pa:{approval['id']}")
    assert "Paste this into the bid form" in tg.sent[-2]["text"]
    assert "freelancer.com/projects/88231" in tg.sent[-2]["text"]
    assert button_data(tg.sent[-1]) == [f"pa:{approval['id']}", f"ps:{approval['id']}", f"px:{approval['id']}"]
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"  # nothing recorded until you say so

    press(bot, f"ps:{approval['id']}")
    assert ctx.db.get_job("freelancer:88231")["state"] == "submitted"
    assert button_data(tg.sent[-1]) == ["won:freelancer:88231"]

    monkeypatch.setattr("freelance_agents.telegram_bot.DeliveryPipeline.run", lambda self, jid: "delivered: x.zip")
    press(bot, "won:freelancer:88231")
    assert ctx.db.get_job("freelancer:88231")["state"] == "planning"
    assert tg.sent[-1]["text"] == "🔨 Build freelancer:88231: delivered: x.zip"


class FakeFiller:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def fill(self, url, amount, days, text):
        if self.error:
            raise self.error
        self.calls.append((url, amount, days, text))
        return url


def test_approve_fills_form_on_pc(setup):
    ctx, tg, bot = setup
    bot.bid_filler = FakeFiller()
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    press(bot, f"pa:{approval['id']}")
    url, amount, days, text = bot.bid_filler.calls[0]
    assert "88231" in url and amount == 150 and days == 2 and "AI-assisted" in text
    assert tg.sent[-1]["text"].startswith("🖥️ Bid form filled on your PC")
    assert button_data(tg.sent[-1]) == [f"ps:{approval['id']}", f"px:{approval['id']}"]
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"


def test_form_is_prefilled_before_the_card_is_sent(setup):
    ctx, tg, bot = setup
    bot.bid_filler = FakeFiller()
    bot.notify_pending()
    fl_card = card(tg, FL_TITLE)
    assert bot.bid_filler.calls and bot.bid_filler.calls[0][1] == 150   # filled first
    assert "Also filled in Chrome on your PC" in fl_card["text"]
    assert [d.split(":")[0] for d in button_data(fl_card)] == ["url", "pe", "ps", "px"]
    upwork_card = card(tg, UP_TITLE)
    assert "Chrome" not in upwork_card["text"]          # not a Freelancer job: no PC fill


def test_prefill_failure_is_shown_on_card(setup):
    from freelance_agents.bid_filler import SignInNeeded
    ctx, tg, bot = setup
    bot.bid_filler = FakeFiller(SignInNeeded("Sign in to Freelancer in the Chrome window"))
    bot.notify_pending()
    fl_card = card(tg, FL_TITLE)
    assert "PC form not filled: Sign in to Freelancer" in fl_card["text"]
    assert button_data(fl_card)[0].startswith("url:")   # phone route still works


def test_fill_failure_falls_back_to_copy(setup):
    from freelance_agents.bid_filler import SignInNeeded
    ctx, tg, bot = setup
    bot.bid_filler = FakeFiller(SignInNeeded("Sign in to Freelancer"))
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    press(bot, f"pa:{approval['id']}")
    texts = [m["text"] for m in tg.sent[-3:]]
    assert texts[0].startswith("⚠️ Couldn't fill") and texts[1].startswith("📋 Paste this")


def test_submitted_is_checked_against_freelancer(setup):
    from types import SimpleNamespace
    ctx, tg, bot = setup
    bids = []
    bot.api_factory = lambda: SimpleNamespace(my_bids=lambda pid: bids)
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    press(bot, f"ps:{approval['id']}")
    assert tg.sent[-1]["text"].startswith("🔎 I can't see your bid")
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"
    bids.append({"id": 1})
    press(bot, f"ps:{approval['id']}")
    assert ctx.db.get_job("freelancer:88231")["state"] == "submitted"
    ctx.db.undo_submitted("freelancer:88231")
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"
    assert any(a["id"] == approval["id"] for a in ctx.db.pending_approvals("proposal"))


class FakeBidAPI:
    def __init__(self):
        self.placed = []

    def self_id(self):
        return 555

    def my_bids(self, pid):
        return [{"id": 7}] if self.placed else []

    def place_bid(self, **kw):
        self.placed.append(kw)
        return {"id": 7}


def test_one_tap_place_bid_now(setup):
    ctx, tg, bot = setup
    ctx.cfg["dry_run"] = False
    ctx.cfg["freelancer"]["bid_via_api"] = True
    api = FakeBidAPI()
    bot.api_factory = lambda: api
    bot.bid_filler = FakeFiller()
    bot.notify_pending()
    pbc = button_data(card(tg, FL_TITLE))[1]
    assert pbc.startswith("pbc:")                 # right under the phone link
    aid = pbc.split(":")[1]

    # edit the proposal first: the bid must use the edited text
    press(bot, f"pe:{aid}")
    edited = "Edited proposal for your price scraper. " + "detail " * 120 + "This is an AI-assisted workflow."
    say(bot, edited)

    assert api.placed == []
    press(bot, f"pbc:{aid}")                      # one tap places it
    assert len(api.placed) == 1
    bid = api.placed[0]
    assert bid["project_id"] == 88231 and bid["amount"] == 150 and bid["description"] == edited
    assert ctx.db.get_job("freelancer:88231")["state"] == "submitted"
    assert button_data(tg.sent[-1]) == ["won:freelancer:88231"]

    press(bot, f"pbc:{aid}")                      # a second tap can't double-bid
    assert len(api.placed) == 1 and tg.sent[-1]["text"].startswith("✅ You already bid on this job (bid id 7)")


def test_buttons_on_already_bid_job_explain_instead_of_error(setup):
    ctx, tg, bot = setup
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    Orchestrator(ctx).approve("freelancer:88231")          # e.g. recorded by the manual-bid sync
    ctx.db.set_bid_status("freelancer:88231", "submitted", "499333120")
    for action in ("pb", "pbc", "pa", "ps"):
        press(bot, f"{action}:{approval['id']}")
        assert tg.sent[-1]["text"] == "✅ You already bid on this job (bid id 499333120). Nothing more to do here."


def test_no_place_bid_button_when_disabled(setup):
    ctx, tg, bot = setup
    bot.bid_filler = FakeFiller()
    bot.notify_pending()
    assert not any(d.startswith(("pb:", "pbc:")) for d in button_data(card(tg, FL_TITLE)))


def test_skip(setup):
    ctx, tg, bot = setup
    approval = ctx.db.pending_approvals("proposal")[0]
    press(bot, f"px:{approval['id']}")
    assert ctx.db.get_job(approval["job_id"])["state"] == "rejected"


def test_edit_proposal_runs_guardrails(setup):
    ctx, tg, bot = setup
    approval = next(a for a in ctx.db.pending_approvals("proposal") if a["job_id"] == "freelancer:88231")
    press(bot, f"pe:{approval['id']}")
    say(bot, "Too short, email me at bob@mail.com")
    assert tg.sent[-1]["text"].startswith("🚫 Edit not saved")
    press(bot, f"pe:{approval['id']}")
    good = "I will build your price scraper. " + "detail " * 130 + "This is an AI-assisted workflow."
    say(bot, good)
    assert json.loads(ctx.db.get_approval(approval["id"])["payload"])["proposal_text"] == good


def test_reply_send_dry_run(setup):
    ctx, tg, bot = setup
    ctx.db.enqueue_approval("freelancer:88231", "reply", {
        "reply_text": "Happy to help.", "send_allowed": True, "thread_id": 1, "message_id": 9,
        "client_message": "Can you start today?", "intent_detected": "question", "escalate": False})
    bot.notify_pending()
    card = tg.sent[-1]
    assert card["text"].startswith("💬 Client message")
    press(bot, button_data(card)[0])
    assert "NOT sent" in tg.sent[-1]["text"]


def test_status_command(setup):
    ctx, tg, bot = setup
    say(bot, "/status")
    assert "awaiting_approval: 2" in tg.sent[-1]["text"]
