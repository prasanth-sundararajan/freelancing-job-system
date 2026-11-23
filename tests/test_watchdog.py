from datetime import datetime, timedelta, timezone

from freelance_agents.agents import ScoutAgent
from freelance_agents.db import now
from freelance_agents.schemas import OpsDiagnosis
from freelance_agents.watchdog import Watchdog


def at(day: int, hh: int, mm: int = 0) -> datetime:
    """Local time on 2026-11-<day>."""
    return datetime(2026, 11, day, hh, mm).astimezone()


NOW = at(23, 15, 0)          # 3 PM; slots are 14:00, 18:00, 22:00


class FakeSystem:
    chrome_enabled = True

    def __init__(self, **state):
        self.state = {"chrome_up": True, "signed_in": True, "task": True,
                      "last_cycle": (at(23, 14, 1), {"scout": 0, "run": 0}),
                      "freelancer": (True, ""), "balance": 9.5, "errors": [], "cycle_rc": 0, **state}
        self.calls = []

    def restart_bot(self): self.calls.append("restart_bot")
    def chrome_up(self): return self.state["chrome_up"]
    def start_chrome(self): self.calls.append("start_chrome")
    def chrome_signed_in(self): return self.state["signed_in"]
    def task_exists(self): return self.state["task"]
    def create_task(self): self.calls.append("create_task"); return True
    def last_cycle(self): return self.state["last_cycle"]
    def run_cycle(self): self.calls.append("run_cycle"); return self.state["cycle_rc"]
    def freelancer_ok(self): return self.state["freelancer"]
    def deepseek_balance(self): return self.state["balance"]
    def recent_errors(self): return self.state["errors"]


def wd(make_ctx, responses=None, clock=NOW, **state):
    ctx = make_ctx(responses or {})
    ctx.cfg["schedule"] = {"cycle_times": ["14:00", "18:00", "22:00"], "missed_grace_minutes": 45}
    ctx.db.kv_set("bot_heartbeat", now())
    sent = []
    w = Watchdog(ctx, FakeSystem(**state), notify=sent.append)
    w.clock = lambda: clock
    return w, sent


def again(w, sent):
    w2 = Watchdog(w.ctx, w.sys, notify=sent.append)
    w2.clock = w.clock
    return w2


def test_healthy_system_does_nothing(make_ctx):
    w, sent = wd(make_ctx)
    r = w.run()
    assert (r.fixed, r.alerts, w.sys.calls, sent) == ([], [], [], [])


def test_dead_bot_is_restarted_once_then_alerts(make_ctx):
    w, sent = wd(make_ctx)
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    w.db.kv_set("bot_heartbeat", old)
    w.run()
    assert w.sys.calls == ["restart_bot"] and "Telegram bot silent" in sent[0]
    # still dead on the next runs: no restart loop, an alert instead
    again(w, sent).run()
    again(w, sent).run()
    assert w.sys.calls == ["restart_bot"]
    assert any("still broken" in s for s in sent)


def test_chrome_closed_and_task_missing_are_fixed(make_ctx):
    w, sent = wd(make_ctx, chrome_up=False, task=False)
    r = w.run()
    assert "start_chrome" in w.sys.calls and "create_task" in w.sys.calls
    assert len(r.fixed) == 2 and sent and "Fixed automatically" in sent[0]


def test_missed_2pm_slot_is_run_at_3pm(make_ctx):
    w, sent = wd(make_ctx, last_cycle=(at(22, 22, 1), {"scout": 0}))     # last run: yesterday 22:00
    w.run()
    assert w.sys.calls == ["run_cycle"] and "Missed the 14:00 job search" in sent[0]


def test_within_grace_period_waits(make_ctx):
    w, _ = wd(make_ctx, clock=at(23, 14, 20), last_cycle=(at(22, 22, 1), {"scout": 0}))
    w.run()
    assert w.sys.calls == []                 # the 14:00 task may still be starting


def test_no_search_outside_the_schedule(make_ctx):
    w, _ = wd(make_ctx, clock=at(24, 9, 0), last_cycle=(at(23, 22, 1), {"scout": 0}))   # 9 AM next day
    w.run()
    assert w.sys.calls == []                 # 22:00 ran; next slot is 14:00


def test_failed_cycle_is_rerun(make_ctx):
    w, _ = wd(make_ctx, last_cycle=(at(23, 14, 1), {"scout": 0, "run": 1}))
    w.run()
    assert w.sys.calls == ["run_cycle"]


def test_slot_math():
    from types import SimpleNamespace
    w = Watchdog(SimpleNamespace(cfg={"schedule": {"cycle_times": ["14:00", "18:00", "22:00"]}, "ops": {}}, db=None),
                 FakeSystem())
    assert w.last_due_slot(at(23, 15)) == at(23, 14)
    assert w.last_due_slot(at(23, 23)) == at(23, 22)
    assert w.last_due_slot(at(24, 9)) == at(23, 22)


def test_owner_only_problems_are_alerts_not_fixes(make_ctx):
    w, sent = wd(make_ctx, signed_in=False, freelancer=(False, "HTTP 401: invalid token"), balance=0.5)
    r = w.run()
    assert r.fixed == [] and len(r.alerts) == 3
    text = sent[0]
    assert "signed out" in text and "token" in text and "$0.50" in text
    # not repeated on the next run
    assert again(w, sent).run().alerts == []


def test_transient_freelancer_error_is_not_alerted(make_ctx):
    w, _ = wd(make_ctx, freelancer=(False, "Connection timed out"))
    assert w.run().alerts == []


def test_stuck_build_is_stopped_and_transient_failure_retried(make_ctx, sample_jobs):
    w, sent = wd(make_ctx)
    ScoutAgent(w.ctx).ingest("file", sample_jobs)
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    w.db._exec("UPDATE jobs SET state='building', updated_at=? WHERE job_id='freelancer:88231'", (old,))
    w.db._exec("UPDATE jobs SET state='escalated' WHERE job_id='upwork:66010'")
    w.db.log("orchestrator", "transition", "upwork:66010", "qualified -> escalated: failed 3 times: APIConnectionError: Connection error")
    w.run()
    assert w.db.get_job("freelancer:88231")["state"] == "escalated"
    assert w.db.get_job("upwork:66010")["state"] == "qualified"      # retried once
    w.db._exec("UPDATE jobs SET state='escalated' WHERE job_id='upwork:66010'")
    again(w, sent).run()
    assert w.db.get_job("upwork:66010")["state"] == "escalated"      # never retried twice


def test_non_transient_escalation_is_left_alone(make_ctx, sample_jobs):
    w, _ = wd(make_ctx)
    ScoutAgent(w.ctx).ingest("file", sample_jobs[:1])
    w.db._exec("UPDATE jobs SET state='escalated' WHERE job_id='freelancer:88231'")
    w.db.log("orchestrator", "transition", "freelancer:88231", "estimated -> escalated: proposal is 40 words; must be 120-250")
    w.run()
    assert w.db.get_job("freelancer:88231")["state"] == "escalated"


def test_unknown_error_gets_ai_diagnosis_and_safe_fix(make_ctx):
    diag = OpsDiagnosis(summary="Model API unreachable", likely_cause="network outage", safe_auto_fix="rerun_cycle")
    w, sent = wd(make_ctx, {"ops": lambda _: diag}, errors=["run failed: APIConnectionError: Connection error."])
    w.run()
    assert w.sys.calls == ["run_cycle"] and any("AI: Model API unreachable" in s for s in sent)
    calls = len(w.ctx.llm.calls)
    again(w, sent).run()                                      # same errors: not diagnosed again
    assert len(w.ctx.llm.calls) == calls


def test_ai_cannot_pick_a_fix_outside_the_allowlist(make_ctx):
    w, _ = wd(make_ctx)
    assert w.fix("delete_database", lambda: True, "x") is False
    assert w.report.alerts and "isn't an allowed fix" in w.report.alerts[0]
