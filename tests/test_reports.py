from datetime import date, datetime

from freelance_agents.db import DB
from freelance_agents.reports import collect, format_report, local_day_bounds_utc


def log_at(db, local_dt: datetime, agent, action, detail=None, cost=0.0):
    ts = local_dt.astimezone().astimezone(__import__("datetime").timezone.utc).isoformat(timespec="seconds")
    db._exec("INSERT INTO audit_log (ts, job_id, agent, action, detail, cost_usd) VALUES (?,?,?,?,?,?)",
             (ts, "j", agent, action, detail, cost))


def test_collect_counts_only_that_local_day():
    db = DB(":memory:")
    day = date(2026, 11, 23)
    log_at(db, datetime(2026, 11, 23, 9, 0), "qualifier", "llm_call", cost=0.001)
    log_at(db, datetime(2026, 11, 23, 9, 1), "proposal_writer", "llm_call", cost=0.004)
    log_at(db, datetime(2026, 11, 23, 9, 2), "orchestrator", "transition", "proposed -> awaiting_approval: queued")
    log_at(db, datetime(2026, 11, 23, 23, 59), "orchestrator", "transition", "awaiting_approval -> submitted: human")
    log_at(db, datetime(2026, 11, 23, 8, 0), "scout", "job_discovered")
    log_at(db, datetime(2026, 11, 22, 23, 0), "qualifier", "llm_call", cost=0.5)     # yesterday
    log_at(db, datetime(2026, 11, 24, 0, 1), "qualifier", "llm_call", cost=0.5)      # tomorrow
    d = collect(db, day)
    assert round(d["cost_total"], 4) == 0.005 and d["llm_calls"] == 2
    assert list(d["cost_by_agent"]) == ["proposal_writer", "qualifier"]
    assert (d["jobs_found"], d["proposals"], d["bids"]) == (1, 1, 1)
    assert round(d["month_cost"], 3) == 0.505                                         # 22nd + 23rd


def test_format_shows_balance_change():
    data = {"day": "2026-11-23", "cost_total": 0.0123, "cost_by_agent": {"qualifier": 0.0123}, "llm_calls": 3,
            "jobs_found": 15, "proposals": 1, "bids": 0, "builds": 0, "month_cost": 0.25}
    text = format_report(data, 9.40, 9.52)
    assert "AI cost today (est.): $0.0123" in text and "DeepSeek balance: $9.40 (used $0.12" in text
    assert "Jobs found: 15" in text
    assert "top-up" in format_report(data, 15.0, 9.52)


def test_day_bounds_are_24h():
    a, b = local_day_bounds_utc(date(2026, 11, 23))
    assert (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() == 86400
