"""Telegram approval bot.

Every item in the approval queue becomes a Telegram message with buttons:

    proposal  -> the bid form is filled in Chrome on your PC BEFORE the card is sent; card buttons:
                 I placed the bid / Edit (re-fills) / Fill again / Skip. Without the filler: Approve sends text to paste.
                 then "I submitted it" -> recorded as submitted, then "Won" -> build starts
    reply     -> Send (via Freelancer messaging API) / Edit / Ignore
    questions -> "Asked the client"
    delivery  -> zip + handoff notes, Approve / Rework

Bids are never placed by the bot (Freelancer prohibits automatic bidders).
Only the paired owner account can press buttons or send commands.
"""
from __future__ import annotations

import html
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from . import guardrails as g
from .agents import Ctx
from .bid_sync import sync_bids
from .db import now
from .delivery import DeliveryPipeline
from .inbox import send_reply
from .orchestrator import Orchestrator
from .reports import build_report
from .schemas import EstimatorResult, ProposalResult

MAX_TEXT = 3900


class TelegramClient:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.http = http or httpx.Client(timeout=70)

    def _call(self, method: str, **kwargs) -> dict:
        resp = self.http.post(self.base + method, **kwargs)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description')}")
        return body["result"]

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call("getUpdates", json={"offset": offset, "timeout": timeout,
                                              "allowed_updates": ["message", "callback_query"]})

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             html: bool = False) -> dict:
        """buttons: (label, callback_data) or (label, "url:https://...") for a link button."""
        payload: dict = {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": t, "url": d[4:]} if d.startswith("url:") else {"text": t, "callback_data": d} for t, d in row]
                for row in buttons]}
        return self._call("sendMessage", json=payload)

    def send_document(self, chat_id: int, path: str, caption: str = "") -> dict:
        with open(path, "rb") as f:
            return self._call("sendDocument", data={"chat_id": chat_id, "caption": caption[:1000]},
                              files={"document": (Path(path).name, f)})

    def answer(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", json={"callback_query_id": callback_id, "text": text[:190]})

    def clear_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            self._call("editMessageReplyMarkup", json={"chat_id": chat_id, "message_id": message_id,
                                                       "reply_markup": {"inline_keyboard": []}})
        except RuntimeError:
            pass


class ApprovalBot:
    def __init__(self, ctx: Ctx, tg: TelegramClient, owner_id: int | None, api_factory, ctx_factory=None,
                 on_pair=None, background_builds: bool = True, bid_filler=None):
        self.ctx, self.tg, self.owner_id = ctx, tg, owner_id
        self.bid_filler = bid_filler                # fills the bid form in Chrome; never clicks Place Bid
        self.api_factory = api_factory              # () -> FreelancerAPI (only when needed)
        self.ctx_factory = ctx_factory              # () -> new Ctx, for background builds (own DB connection)
        self.on_pair = on_pair                      # (owner_id) -> None, e.g. save to .env
        self.background_builds = background_builds
        self.pair_code = None if owner_id else secrets.token_hex(3)
        self.offset: int | None = None

    @property
    def db(self):
        return self.ctx.db

    # --- loop --------------------------------------------------------------------
    def poll_once(self, timeout: int = 30) -> None:
        self.db.kv_set("bot_heartbeat", now())   # the Ops agent restarts the bot if this goes stale
        for update in self.tg.get_updates(self.offset, timeout=timeout):
            self.offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception as e:  # never let one bad update stop the bot
                if self.owner_id:
                    self.tg.send(self.owner_id, f"⚠️ Error: {type(e).__name__}: {e}")
        if self.owner_id:
            self.notify_pending()
            self.notify_escalations()
            self.maybe_sync_bids()
            self.maybe_daily_report()

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except (httpx.HTTPError, RuntimeError) as e:
                print(f"telegram: {e}; retrying in 10s")
                self.db.kv_set("bot_heartbeat", now())  # network blips aren't a frozen bot
                time.sleep(10)

    # --- updates -----------------------------------------------------------------
    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            if cq["from"]["id"] != self.owner_id:
                self.tg.answer(cq["id"], "Not allowed")
                return
            action, _, arg = cq.get("data", "").partition(":")
            self.tg.answer(cq["id"])
            self.tg.clear_buttons(cq["message"]["chat"]["id"], cq["message"]["message_id"])
            self.on_button(action, arg)
            return
        msg = update.get("message") or {}
        user_id, text = (msg.get("from") or {}).get("id"), (msg.get("text") or "").strip()
        if self.owner_id is None:
            if self.pair_code and text == f"/start {self.pair_code}":
                self.owner_id, self.pair_code = user_id, None
                if self.on_pair:
                    self.on_pair(user_id)
                self.tg.send(user_id, "✅ Paired. You'll get proposals, client replies and deliveries here. /help for commands.")
            return
        if user_id != self.owner_id:
            return
        self.on_text(text)

    def on_text(self, text: str) -> None:
        editing = self.db.kv_get("tg_editing")
        if text in ("/start", "/help"):
            self.tg.send(self.owner_id, "Commands:\n/status - job counts and AI cost\n/report - today's cost report "
                                        "(/report yesterday)\n/queue - resend everything waiting for you\n"
                                        "/cancel - cancel an edit\nButtons on each message do the rest.")
        elif text == "/status":
            rows = self.db.all_jobs()
            counts: dict[str, int] = {}
            for r in rows:
                counts[r["state"]] = counts.get(r["state"], 0) + 1
            lines = [f"{k}: {v}" for k, v in sorted(counts.items())]
            dry = " (dry run: replies are NOT sent)" if self.ctx.cfg["dry_run"] else ""
            self.tg.send(self.owner_id, "📊 Jobs\n" + "\n".join(lines) +
                         f"\nAI cost: ${sum(r['cost_usd'] for r in rows):.3f}{dry}")
        elif text in ("/report", "/report yesterday"):
            day = date.today() - timedelta(days=1 if text.endswith("yesterday") else 0)
            self.tg.send(self.owner_id, build_report(self.db, day, os.getenv("DEEPSEEK_API_KEY"), remember_balance=False))
        elif text == "/queue":
            self.db._exec("DELETE FROM telegram_notified WHERE approval_id IN (SELECT id FROM approval_queue WHERE status='pending')")
            self.notify_pending()
        elif text == "/cancel":
            self.db.kv_set("tg_editing", None)
            self.tg.send(self.owner_id, "Edit cancelled.")
        elif editing and not text.startswith("/"):
            kind, _, approval_id = editing.partition(":")
            self.db.kv_set("tg_editing", None)
            self.apply_edit(kind, int(approval_id), text)
        else:
            self.tg.send(self.owner_id, "Use the buttons, or /help.")

    # --- buttons -----------------------------------------------------------------
    def on_button(self, action: str, arg: str) -> None:
        orch = Orchestrator(self.ctx)
        if action == "won":
            self.start_build(arg)
            return
        approval = self.db.get_approval(int(arg))
        if approval is None:
            self.tg.send(self.owner_id, "That item no longer exists.")
            return
        p, job_id = json.loads(approval["payload"]), approval["job_id"]

        proposal_actions = {"pa", "pb", "pbc", "pbx", "ps", "px", "pe"}
        job_row = self.db.get_job(job_id)
        if action in proposal_actions and job_row and job_row["state"] != "awaiting_approval":
            bid = self.db.conn.execute("SELECT platform_bid_id FROM bids WHERE job_id=?", (job_id,)).fetchone()
            if job_row["state"] in ("submitted", "negotiating", "won", "planning", "building", "qa", "delivered"):
                bid_txt = f" (bid id {bid[0]})" if bid and bid[0] else ""
                self.tg.send(self.owner_id, f"✅ You already bid on this job{bid_txt}. Nothing more to do here.",
                             [[("🏆 I won this job", f"won:{job_id}")]])
            else:
                self.tg.send(self.owner_id, f"This proposal is closed (job is '{job_row['state']}').")
            return

        if action == "pa":       # proposal approved -> fill the form on the PC (or text to paste)
            job = json.loads(self.db.get_job(job_id)["data"])
            amount, days = p["bid_price"]["amount"], p["timeline_days"]
            filled = False
            if self.bid_filler is not None and job.get("platform") == "freelancer":
                try:
                    self.bid_filler.fill(job["url"], amount, days, p["proposal_text"])
                    filled = True
                    self.tg.send(self.owner_id,
                                 f"🖥️ Bid form filled on your PC and checked: amount {amount:g} {p['bid_price']['currency']}, "
                                 f"{days} days, proposal ({len(p['proposal_text'])} characters).\n"
                                 f"Check it and click PLACE BID in the Chrome window, then tap below.\n{job['url']}",
                                 [[("📨 I placed the bid", f"ps:{arg}"), ("❌ Skip", f"px:{arg}")]])
                except Exception as e:  # SignInNeeded or anything else -> fall back to copy/paste
                    self.tg.send(self.owner_id, f"⚠️ Couldn't fill the form automatically: {e}")
            if not filled:
                self.tg.send(self.owner_id, f"📋 Paste this into the bid form:\n{job['url']}\n\n"
                                            f"Bid amount: {amount:g} {p['bid_price']['currency']}\n"
                                            f"Delivered in: {days} days")
                self.tg.send(self.owner_id, p["proposal_text"],
                             [[("🔁 Try filling again", f"pa:{arg}")],
                              [("📨 I submitted it", f"ps:{arg}"), ("❌ Skip", f"px:{arg}")]])
        elif action == "pb":     # place bid via API: ask once to confirm
            job = json.loads(self.db.get_job(job_id)["data"])
            self.tg.send(self.owner_id, f"🚀 Place this bid on Freelancer now?\n{job.get('title', job_id)}\n"
                                        f"{p['bid_price']['amount']:g} {p['bid_price']['currency']}, {p['timeline_days']} days",
                         [[("✅ Yes, place it", f"pbc:{arg}"), ("✖️ Cancel", f"pbx:{arg}")]])
        elif action == "pbc":    # confirmed: one bid through the official API
            try:
                message = orch.approve(job_id, submit=True, api=self.api_factory())
            except Exception as e:
                self.tg.send(self.owner_id, f"🚫 Bid NOT placed: {e}", [[("🚀 Try again", f"pb:{arg}")]])
                return
            self.tg.send(self.owner_id, f"✅ {message}\nYou can close that project's tab in Chrome.\n"
                                        "Tap when the client awards you:", [[("🏆 I won this job", f"won:{job_id}")]])
        elif action == "pbx":
            self.tg.send(self.owner_id, "Cancelled. The card's buttons are still there if you change your mind.",
                         [[("🚀 Place bid", f"pb:{arg}"), ("❌ Skip", f"px:{arg}")]])
        elif action == "ps":     # human submitted it on Freelancer
            if job_id.startswith("freelancer:"):
                try:
                    bids = self.api_factory().my_bids(int(job_id.split(":", 1)[1]))
                except Exception as e:
                    bids = None
                    self.tg.send(self.owner_id, f"⚠️ Couldn't check Freelancer ({e}); recording it anyway.")
                if bids == []:
                    self.tg.send(self.owner_id, "🔎 I can't see your bid on Freelancer yet. Click PLACE BID on the "
                                                "project page first, then tap again.",
                                 [[("📨 I submitted it", f"ps:{arg}"), ("🔁 Fill the form again", f"pa:{arg}")]])
                    return
            orch.approve(job_id)
            self.tg.send(self.owner_id, f"✅ Recorded as submitted: {job_id}\nTap when the client awards you:",
                         [[("🏆 I won this job", f"won:{job_id}")]])
        elif action == "px":
            if self.db.get_job(job_id)["state"] == "awaiting_approval":
                orch.reject(job_id)
            self.tg.send(self.owner_id, f"Skipped {job_id}.")
        elif action in ("pe", "re"):
            self.db.kv_set("tg_editing", f"{'proposal' if action == 'pe' else 'reply'}:{arg}")
            self.tg.send(self.owner_id, "✏️ Send the new text as your next message (/cancel to stop).")
        elif action == "rs":
            api = None if self.ctx.cfg["dry_run"] else self.api_factory()
            self.tg.send(self.owner_id, f"💬 {send_reply(self.ctx, api, int(arg))}")
        elif action == "rx":
            self.db.decide_approval_id(int(arg), "ignored")
            self.tg.send(self.owner_id, "Reply ignored.")
        elif action == "qa":
            self.db.decide_approval_id(int(arg), "asked")
            self.tg.send(self.owner_id, f"Noted. When the client answers, the inbox picks it up; then tap Build.",
                         [[("🔨 Build now", f"won:{job_id}")]])
        elif action == "da":
            self.db.decide_approval_id(int(arg), "approved")
            self.tg.send(self.owner_id, f"📦 Approved. Upload {Path(p['zip']).name} to the client on Freelancer.")
        elif action == "dr":
            self.db.decide_approval_id(int(arg), "rework")
            self.tg.send(self.owner_id, "Marked for rework. Tell the agents what to change by replying in the client thread, then run build again.")

    def apply_edit(self, kind: str, approval_id: int, text: str) -> None:
        approval = self.db.get_approval(approval_id)
        if approval is None or approval["status"] != "pending":
            self.tg.send(self.owner_id, "That item is no longer pending.")
            return
        payload = json.loads(approval["payload"])
        if kind == "reply":
            try:
                api = None if self.ctx.cfg["dry_run"] else self.api_factory()
                self.tg.send(self.owner_id, f"💬 {send_reply(self.ctx, api, approval_id, text)}")
            except g.GuardrailViolation as e:
                self.tg.send(self.owner_id, f"🚫 Not sent: {e}")
            return
        est = EstimatorResult.model_validate(self.db.latest_output(approval["job_id"], "estimator"))
        proposal = ProposalResult.model_validate({**payload, "proposal_text": text})
        _, errors = g.check_proposal(proposal, est, [i["url"] for i in self.ctx.portfolio])
        if errors:
            self.tg.send(self.owner_id, "🚫 Edit not saved:\n- " + "\n- ".join(errors))
            return
        self.db.update_approval_payload(approval_id, {**payload, "proposal_text": text})
        self.db.tg_mark(approval_id, None)
        self.db._exec("DELETE FROM telegram_notified WHERE approval_id=?", (approval_id,))
        self.notify_pending()

    def start_build(self, job_id: str) -> None:
        row = self.db.get_job(job_id)
        if row is None:
            self.tg.send(self.owner_id, f"Unknown job {job_id}")
            return
        try:
            if row["state"] in ("submitted", "negotiating"):
                DeliveryPipeline(self.ctx).start(job_id, notes="started from Telegram")
        except ValueError as e:
            self.tg.send(self.owner_id, f"Can't start: {e}")
            return
        self.tg.send(self.owner_id, f"🔨 Building {job_id}… I'll message you when QA is done.")

        def work():
            ctx = self.ctx_factory() if self.ctx_factory else self.ctx
            try:
                result = DeliveryPipeline(ctx).run(job_id)
            except Exception as e:
                result = f"failed: {type(e).__name__}: {e}"
            self.tg.send(self.owner_id, f"🔨 Build {job_id}: {result}")

        if self.background_builds:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()

    # --- notifications -------------------------------------------------------------
    def notify_pending(self) -> None:
        for item in self.db.pending_approvals():
            if self.db.tg_notified(item["id"]):
                continue
            p, i, job_id = json.loads(item["payload"]), item["id"], item["job_id"]
            row = self.db.get_job(job_id)
            job = json.loads(row["data"]) if row else {}
            sent = None
            if item["kind"] == "proposal":
                sent = self.send_proposal_card(i, job_id, job, p)
            elif item["kind"] == "reply":
                status = "OK to send" if p.get("send_allowed") else f"BLOCKED: {p.get('escalation_reason')}"
                buttons = [[("✅ Send", f"rs:{i}")]] if p.get("send_allowed") else []
                buttons = [buttons[0] + [("✏️ Edit", f"re:{i}"), ("❌ Ignore", f"rx:{i}")]] if buttons else \
                          [[("✏️ Write my own", f"re:{i}"), ("❌ Ignore", f"rx:{i}")]]
                sent = self.tg.send(self.owner_id,
                    f"💬 Client message #{i} — {job.get('title', job_id)}\n\nCLIENT: {p.get('client_message', '')}\n\n"
                    f"DRAFT ({status}):\n{p['reply_text']}", buttons)
            elif item["kind"] == "questions":
                qs = "\n".join(f"- {q}" for q in p.get("questions", []))
                sent = self.tg.send(self.owner_id, f"❓ Ask the client before building #{i} ({job.get('title', job_id)}):\n{qs}",
                                    [[("✅ Asked the client", f"qa:{i}")]])
            elif item["kind"] == "delivery":
                checks = "".join(f"\n⚠️ {c}" for c in p.get("check_before_sending") or [])
                sent = self.tg.send(self.owner_id, f"📦 Delivery ready #{i} — {job.get('title', job_id)}{checks}\n\n{p['handoff_notes_md']}",
                                    [[("✅ Approve", f"da:{i}"), ("🔁 Rework", f"dr:{i}")]])
                if Path(p["zip"]).exists():
                    self.tg.send_document(self.owner_id, p["zip"], f"Package for {job_id}")
            self.db.tg_mark(i, (sent or {}).get("message_id"))

    def send_proposal_card(self, i: int, job_id: str, job: dict, p: dict) -> dict:
        """Card with two ways to bid:
        1. 📱 Open bid page on your phone + tap-to-copy amount / days / proposal, then Place Bid there.
        2. 🚀 Place bid now: one tap, posted through Freelancer's API (when bid_via_api is on).
        Freelancer doesn't keep half-filled bid forms, so a link can't open pre-filled; the PC
        Chrome form (if enabled) is filled too, for review on a computer."""
        e = html.escape
        b, comp = job.get("budget", {}), job.get("competition") or {}
        amount, cur, days = p["bid_price"]["amount"], p["bid_price"]["currency"], p["timeline_days"]
        competition = f" | {comp['bid_count']} bids, avg {comp['bid_avg']:.0f}" if comp.get("bid_avg") else ""
        is_freelancer = job.get("platform") == "freelancer"
        api_bids = bool(self.ctx.cfg.get("freelancer", {}).get("bid_via_api")) and is_freelancer
        pc = ""
        if self.bid_filler is not None and is_freelancer:
            result = self.prefill(job, p)
            pc = ("🖥️ Also filled in Chrome on your PC.\n" if result is True
                  else f"🖥️ PC form not filled: {e(str(result))}\n")
        text = (f"🆕 <b>Proposal #{i}</b>\n{e(job.get('title', job_id))}\n"
                f"Budget {b.get('min', 0):.0f}-{b.get('max', 0):.0f} {e(b.get('currency', ''))}{e(competition)}\n\n"
                f"Bid amount: <code>{amount:g}</code> {e(cur)}\n"
                f"Delivered in: <code>{days}</code> days\n{pc}\n"
                f"Proposal (tap to copy):\n<pre>{e(p['proposal_text'])}</pre>")
        buttons = []
        if job.get("url"):
            buttons.append([("📱 Open bid page", f"url:{job['url']}")])
        if api_bids:
            buttons.append([("🚀 Place bid now", f"pbc:{i}")])
        buttons.append([("✏️ Edit", f"pe:{i}"), ("📨 I placed it myself", f"ps:{i}"), ("❌ Skip", f"px:{i}")])
        return self.tg.send(self.owner_id, text, buttons, html=True)

    def prefill(self, job: dict, payload: dict):
        """Fill the bid form in Chrome before the card is sent. True on success, else the error text."""
        try:
            self.bid_filler.fill(job["url"], payload["bid_price"]["amount"], payload["timeline_days"], payload["proposal_text"])
            return True
        except Exception as e:
            return str(e) or type(e).__name__

    def maybe_daily_report(self, now_local: datetime | None = None) -> None:
        """Send the daily cost/activity report once a day at config telegram.daily_report_hour (local)."""
        now_local = now_local or datetime.now()
        hour = int(self.ctx.cfg.get("telegram", {}).get("daily_report_hour", 21))
        key = f"daily_report:{now_local.date().isoformat()}"
        if now_local.hour < hour or self.db.kv_get(key):
            return
        self.db.kv_set(key, "sent")
        self.tg.send(self.owner_id, build_report(self.db, now_local.date(), os.getenv("DEEPSEEK_API_KEY")))

    def maybe_sync_bids(self, every_s: int = 900) -> None:
        """Every 15 min: record bids you placed yourself; tell you when a client awards you."""
        if time.time() - getattr(self, "_last_sync", 0) < every_s:
            return
        self._last_sync = time.time()
        try:
            result = sync_bids(self.ctx, self.api_factory())
        except Exception as e:
            print(f"bid sync: {e}")
            return
        for job_id in result["recorded"]:
            self.retire_cards(job_id)
            title = json.loads(self.db.get_job(job_id)["data"]).get("title", job_id)
            self.tg.send(self.owner_id, f"📨 Saw your bid on Freelancer for \"{title}\"; recorded it.")
        for a in result["awarded"]:
            job = json.loads(self.db.get_job(a["job_id"])["data"])
            self.tg.send(self.owner_id,
                         f"🏆 You were awarded: {job.get('title', a['job_id'])} ({a['amount']})\n"
                         f"{job.get('url', '')}\nAccept the award on Freelancer, then tap Build.",
                         [[("🔨 Build", f"won:{a['job_id']}")]])

    def retire_cards(self, job_id: str) -> None:
        """Remove the buttons from this job's proposal cards (it's been bid on)."""
        rows = self.db.conn.execute(
            "SELECT t.message_id FROM telegram_notified t JOIN approval_queue a ON a.id=t.approval_id "
            "WHERE a.job_id=? AND a.kind='proposal' AND t.message_id IS NOT NULL", (job_id,)).fetchall()
        for (message_id,) in rows:
            self.tg.clear_buttons(self.owner_id, message_id)

    def notify_escalations(self) -> None:
        since = self.db.kv_get("tg_escalations_since") or now()
        for row in self.db.escalations_since(since):
            self.tg.send(self.owner_id, f"🚨 Needs you: {row['job_id']}\n{row['detail']}")
            since = row["ts"]
        self.db.kv_set("tg_escalations_since", since)
