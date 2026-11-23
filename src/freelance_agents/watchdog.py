"""Ops agent (skills.md section 13): health checks, allowlisted self-fixes, AI diagnosis.

Runs every 15 minutes from its own scheduled task (scripts/watchdog.py), outside the bot,
so it can restart the bot. It never edits code/config/credentials, never bids, never
messages clients, never deletes data. Anything it can't safely fix becomes a Telegram alert.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import guardrails as g
from .agents import Ctx
from .config import ROOT
from .db import now
from .schemas import OpsDiagnosis

ALLOWED_FIXES = {"restart_bot", "restart_chrome", "recreate_task", "rerun_cycle", "retry_job", "stop_stuck_job"}
TRANSIENT = re.compile(r"time(d)? ?out|connection|rate.?limit|\b429\b|\b5\d\d\b|temporar|unavailable|APIConnectionError", re.I)
PIPELINE_STATES = {"discovered", "qualified", "estimated", "proposed"}
ACTIVE_BUILD_STATES = {"planning", "building", "qa"}


def _parse_ts(ts: str) -> datetime:
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age_minutes(ts: str | None) -> float | None:
    if not ts:
        return None
    return (datetime.now(timezone.utc) - _parse_ts(ts)).total_seconds() / 60


# --- the real machine ---------------------------------------------------------------
class WindowsSystem:
    """Everything the watchdog touches on this PC. Tests swap in a fake."""

    STARTUP_LINK = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/FreelanceAgents Telegram Bot.lnk"
    TASK = "FreelanceAgents Cycle"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        fill = cfg.get("freelancer", {}).get("bid_fill", {})
        self.chrome_enabled = bool(fill.get("enabled"))
        self.profile = ROOT / fill.get("browser_profile", "data/browser_profile")
        self.port = int(fill.get("debug_port", 9333))

    def restart_bot(self) -> None:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
                        "Where-Object { $_.CommandLine -like '*telegram_bot.py*' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], capture_output=True, timeout=60)
        if self.STARTUP_LINK.exists():
            os.startfile(str(self.STARTUP_LINK))  # type: ignore[attr-defined]
        else:
            subprocess.Popen([str(ROOT / ".venv/Scripts/pythonw.exe"), str(ROOT / "scripts/telegram_bot.py")],
                             cwd=ROOT, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

    def chrome_up(self) -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{self.port}/json/version", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def start_chrome(self) -> None:
        from .bid_filler import BidFiller
        BidFiller(self.profile, debug_port=self.port)._start_chrome()

    def chrome_signed_in(self) -> bool | None:
        from .bid_filler import BidFiller
        filler = BidFiller(self.profile, debug_port=self.port)
        try:
            page = filler._context().new_page()
            page.goto("https://www.freelancer.com/dashboard", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            signed_in = not filler.signed_out(page)
            page.close()
            return signed_in
        except Exception:
            return None
        finally:
            filler.close()

    def task_exists(self) -> bool:
        """All daily job-search tasks (one per schedule.cycle_times entry) are registered."""
        times = self.cfg.get("schedule", {}).get("cycle_times") or []
        return all(subprocess.run(["schtasks", "/Query", "/TN", f"{self.TASK} {t.replace(':', '')}"],
                                  capture_output=True).returncode == 0 for t in times)

    def create_task(self) -> bool:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/schedule_task.py")], capture_output=True)
        return r.returncode == 0

    def last_cycle(self) -> tuple[datetime | None, dict]:
        logs = sorted((ROOT / "data/logs").glob("cycle_*.log"))
        if not logs:
            return None, {}
        text = logs[-1].read_text(encoding="utf-8", errors="replace")
        starts = re.findall(r"===== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) =====", text)
        codes = re.findall(r"exit codes: (\{.*\})", text)
        when = datetime.strptime(starts[-1], "%Y-%m-%d %H:%M:%S").astimezone() if starts else None
        return when, (ast.literal_eval(codes[-1]) if codes else {})

    def run_cycle(self) -> int:
        try:
            return subprocess.run([sys.executable, str(ROOT / "scripts/run_cycle.py")], cwd=ROOT,
                                  capture_output=True, timeout=45 * 60).returncode
        except subprocess.TimeoutExpired:
            return 124

    def freelancer_ok(self) -> tuple[bool, str]:
        from .cli import freelancer_api
        try:
            freelancer_api(self.cfg).self_id()
            return True, ""
        except Exception as e:
            return False, str(e)

    def deepseek_balance(self) -> float | None:
        from .reports import deepseek_balance
        return deepseek_balance(os.getenv("DEEPSEEK_API_KEY"))

    def recent_errors(self) -> list[str]:
        lines: list[str] = []
        logs = sorted((ROOT / "data/logs").glob("cycle_*.log"))
        if logs:
            block = logs[-1].read_text(encoding="utf-8", errors="replace").split("=====")[-1]
            lines += [l.strip() for l in block.splitlines() if re.search(r"failed|Error|Traceback", l)]
        bot_log = ROOT / "data/logs/telegram_bot.log"
        if bot_log.exists():
            tail = bot_log.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
            # "telegram: ... retrying" lines are normal network blips the bot already handles
            lines += [l.strip() for l in tail if re.search(r"Error|Traceback|failed", l) and not l.startswith("telegram:")]
        return lines[-20:]


# --- the agent ----------------------------------------------------------------------
@dataclass
class Report:
    fixed: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Watchdog:
    name = "ops"

    def __init__(self, ctx: Ctx, system, notify=None, use_llm: bool = True):
        self.ctx, self.sys, self.notify, self.use_llm = ctx, system, notify, use_llm
        self.clock = lambda: datetime.now().astimezone()   # tests replace this
        self.ops = ctx.cfg.get("ops", {})
        self.db = ctx.db
        self.report = Report()

    # --- rate-limited fixes and de-duplicated alerts --------------------------------
    def fix(self, fix: str, action, label: str) -> bool:
        if fix not in ALLOWED_FIXES:
            self.alert(f"fix:{fix}", f"Wanted to run '{fix}', which isn't an allowed fix.")
            return False
        last = self.db.kv_get(f"ops_fix_last:{fix}")
        if last and _age_minutes(last) < 60:
            fails = int(self.db.kv_get(f"ops_fix_fails:{fix}") or 0) + 1
            self.db.kv_set(f"ops_fix_fails:{fix}", str(fails))
            if fails >= 2:
                self.alert(f"fixloop:{fix}", f"{label}: still broken after '{fix}'. Needs a look.")
            return False
        self.db.kv_set(f"ops_fix_last:{fix}", now())
        try:
            ok = action()
        except Exception as e:
            ok = False
            self.report.notes.append(f"{fix} raised {type(e).__name__}: {e}")
        if ok is not False:
            self.db.kv_set(f"ops_fix_fails:{fix}", "0")
            self.report.fixed.append(f"{label} → {fix}")
            self.db.log(self.name, f"fix_{fix}", None, label)
            return True
        self.alert(f"fixfail:{fix}", f"{label}: tried '{fix}' but it failed.")
        return False

    def alert(self, key: str, text: str) -> None:
        last = self.db.kv_get(f"ops_alert:{key}")
        if last and _age_minutes(last) < 60 * float(self.ops.get("alert_repeat_hours", 6)):
            return
        self.db.kv_set(f"ops_alert:{key}", now())
        self.report.alerts.append(text)
        self.db.log(self.name, "alert", None, text[:500])

    # --- checks -------------------------------------------------------------------
    def check_bot(self) -> None:
        age = _age_minutes(self.db.kv_get("bot_heartbeat"))
        if age is None or age > float(self.ops.get("bot_heartbeat_max_minutes", 5)):
            self.fix("restart_bot", self.sys.restart_bot,
                     f"Telegram bot silent for {'ever' if age is None else f'{age:.0f} min'}")

    def check_chrome(self) -> None:
        if not getattr(self.sys, "chrome_enabled", True):
            return
        if not self.sys.chrome_up():
            self.fix("restart_chrome", self.sys.start_chrome, "Bot's Chrome window was closed")
            return
        last = self.db.kv_get("ops_signin_checked")
        if last and _age_minutes(last) < float(self.ops.get("signin_check_minutes", 60)):
            return
        self.db.kv_set("ops_signin_checked", now())
        if self.sys.chrome_signed_in() is False:
            self.alert("signin", "🔐 The bot's Chrome is signed out of Freelancer. Open that Chrome window "
                                 "and sign in (keep it open). Until then cards can't pre-fill.")

    def check_task(self) -> None:
        if not self.sys.task_exists():
            self.fix("recreate_task", self.sys.create_task, "A daily job-search task was missing")

    def last_due_slot(self, now_local: datetime) -> datetime | None:
        """Most recent scheduled run time (schedule.cycle_times, local) at or before now."""
        times = self.ctx.cfg.get("schedule", {}).get("cycle_times") or []
        slots = []
        for days_back in (0, 1):
            day = (now_local - timedelta(days=days_back)).date()
            for t in times:
                hh, mm = (int(x) for x in t.split(":"))
                slots.append(datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm).astimezone())
        past = [s for s in slots if s <= now_local]
        return max(past) if past else None

    def check_cycle(self) -> None:
        """Run a missed slot (PC asleep at 14:00, awake at 15:00) or retry a failed run. Never outside the schedule."""
        now_local = self.clock()
        slot = self.last_due_slot(now_local)
        if slot is None:
            return
        grace = timedelta(minutes=float(self.ctx.cfg.get("schedule", {}).get("missed_grace_minutes", 45)))
        when, codes = self.sys.last_cycle()
        failed = [k for k, v in codes.items() if v not in (0, None)]
        ran_for_slot = when is not None and when >= slot - timedelta(minutes=5)
        if not ran_for_slot and now_local - slot > grace:
            self.fix("rerun_cycle", lambda: self.sys.run_cycle() == 0,
                     f"Missed the {slot:%H:%M} job search (PC asleep?)")
        elif ran_for_slot and failed and now_local - when > timedelta(minutes=30):
            self.fix("rerun_cycle", lambda: self.sys.run_cycle() == 0, f"The {slot:%H:%M} job search had failures: {failed}")

    def check_accounts(self) -> None:
        ok, err = self.sys.freelancer_ok()
        if not ok and not TRANSIENT.search(err):
            self.alert("freelancer_token", f"🔑 Freelancer API refused the token ({err[:120]}). It may have "
                                           "expired: create a new one at accounts.freelancer.com/settings/develop and "
                                           "run scripts\\set_key.py FREELANCER_OAUTH_TOKEN")
        balance = self.sys.deepseek_balance()
        low = float(self.ops.get("low_balance_usd", 2.0))
        if balance is not None and balance < low:
            self.alert("low_balance", f"💳 DeepSeek balance is ${balance:.2f} (below ${low:.2f}). Top up at "
                                      "platform.deepseek.com or the agents will stop.")

    def check_jobs(self) -> None:
        limit_h = float(self.ops.get("stuck_job_hours", 3))
        for row in self.db.all_jobs():
            age = _age_minutes(row["updated_at"]) or 0
            if row["state"] in ACTIVE_BUILD_STATES and age > limit_h * 60:
                def stop(job_id=row["job_id"], state=row["state"]):
                    if not g.can_transition(state, "escalated"):
                        return False
                    self.db.set_state(job_id, "escalated")
                    self.db.log("orchestrator", "transition", job_id, f"{state} -> escalated: stuck {age / 60:.1f} h (ops)")
                    return True
                if self.fix_job("stop_stuck_job", row["job_id"], stop):
                    self.alert(f"stuck:{row['job_id']}", f"⛔ {row['job_id']} was stuck in '{row['state']}' for "
                                                         f"{age / 60:.1f} h; stopped it. Check with /status or rebuild.")
            elif row["state"] == "escalated":
                self.retry_transient(row["job_id"])

    def fix_job(self, fix: str, job_id: str, action) -> bool:
        """Per-job fixes: once per job, ever."""
        key = f"ops_job_fix:{fix}:{job_id}"
        if self.db.kv_get(key):
            return False
        self.db.kv_set(key, now())
        ok = action()
        if ok:
            self.report.fixed.append(f"{job_id} → {fix}")
            self.db.log(self.name, f"fix_{fix}", job_id)
        return bool(ok)

    def retry_transient(self, job_id: str) -> None:
        last = self.db.conn.execute("SELECT detail FROM audit_log WHERE job_id=? AND action='transition' "
                                    "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if not last or not last[0]:
            return
        m = re.match(r"(\w+) -> escalated: (.*)", last[0])
        if not m or m.group(1) not in PIPELINE_STATES or not TRANSIENT.search(m.group(2)):
            return

        def back():
            self.db.set_state(job_id, m.group(1))
            self.db.log("orchestrator", "transition", job_id, f"escalated -> {m.group(1)}: retry after temporary error (ops)")
            return True
        self.fix_job("retry_job", job_id, back)

    def diagnose_unknown(self) -> None:
        errors = [e for e in self.sys.recent_errors() if e]
        if not errors:
            return
        digest = hashlib.sha1("\n".join(errors).encode()).hexdigest()
        if self.db.kv_get("ops_last_error_digest") == digest:
            return  # already looked at these exact errors
        self.db.kv_set("ops_last_error_digest", digest)
        if not self.use_llm:
            self.alert(f"errors:{digest[:8]}", "⚠️ Errors in the logs:\n" + "\n".join(errors[-5:]))
            return
        user = ("Diagnose these log lines from the freelancing system and pick at most one allowlisted fix.\n"
                f"Fixes already applied this run: {self.report.fixed}\n\n<log_lines>\n" + "\n".join(errors) + "\n</log_lines>")
        try:
            d: OpsDiagnosis = self.ctx.llm.structured(agent=self.name, system=self.ctx.skills.prompt_for(self.name),
                                                      user=user, schema=OpsDiagnosis)
        except Exception as e:
            self.alert(f"errors:{digest[:8]}", f"⚠️ Errors in the logs (AI diagnosis failed: {e}):\n" + "\n".join(errors[-5:]))
            return
        finally:
            self.db.log(self.name, "llm_call", None, cost=self.ctx.llm.last_cost)
        actions = {"restart_bot": self.sys.restart_bot, "restart_chrome": self.sys.start_chrome,
                   "recreate_task": self.sys.create_task, "rerun_cycle": lambda: self.sys.run_cycle() == 0}
        applied = d.safe_auto_fix != "none" and self.fix(d.safe_auto_fix, actions[d.safe_auto_fix], f"AI: {d.summary}")
        if d.user_action or not applied:
            self.alert(f"diag:{digest[:8]}", f"🩺 {d.summary}\nCause: {d.likely_cause}"
                       + (f"\nAuto-fix: {d.safe_auto_fix}" if applied else "")
                       + (f"\nYou: {d.user_action}" if d.user_action else ""))

    # --- run ----------------------------------------------------------------------
    def run(self) -> Report:
        for check in (self.check_task, self.check_bot, self.check_chrome, self.check_cycle,
                      self.check_accounts, self.check_jobs, self.diagnose_unknown):
            try:
                check()
            except Exception as e:
                self.report.notes.append(f"{check.__name__} crashed: {type(e).__name__}: {e}")
        self.db.kv_set("ops_last_run", now())
        if self.notify and (self.report.fixed or self.report.alerts):
            parts = []
            if self.report.fixed:
                parts.append("🛠️ Fixed automatically:\n" + "\n".join(f"• {f}" for f in self.report.fixed))
            if self.report.alerts:
                parts.append("\n\n".join(self.report.alerts))
            self.notify("🤖 Ops agent\n" + "\n\n".join(parts))
        return self.report
