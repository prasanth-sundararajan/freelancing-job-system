"""Daily AI-cost and activity report (sent to Telegram).

Two numbers for money:
- Estimated cost: tracked per call from token counts and the prices in config.yaml
  (this project only).
- DeepSeek balance: the real account balance from DeepSeek's API. The key may be
  shared with other projects, so the day-to-day drop can include their usage.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import httpx

from .db import DB


def local_day_bounds_utc(day: date) -> tuple[str, str]:
    """Start/end of a local calendar day, as UTC ISO strings (audit_log stores UTC)."""
    start = datetime.combine(day, time.min).astimezone()          # local tz of this PC
    end = start + timedelta(days=1)
    fmt = lambda d: d.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    return fmt(start), fmt(end)


def deepseek_balance(api_key: str | None, http: httpx.Client | None = None) -> float | None:
    if not api_key:
        return None
    try:
        r = (http or httpx).get("https://api.deepseek.com/user/balance",
                                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                                timeout=20)
        infos = r.json().get("balance_infos") or []
        usd = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
        return float(usd["total_balance"]) if usd else None
    except Exception:
        return None


def collect(db: DB, day: date) -> dict:
    start, end = local_day_bounds_utc(day)
    by_agent: dict[str, float] = defaultdict(float)
    calls = 0
    for agent, cost in db.conn.execute(
            "SELECT agent, cost_usd FROM audit_log WHERE ts>=? AND ts<? AND cost_usd>0", (start, end)):
        by_agent[agent] += cost
        calls += 1
    count = lambda sql: db.conn.execute(sql, (start, end)).fetchone()[0]  # noqa: E731
    month_start, _ = local_day_bounds_utc(day.replace(day=1))
    return {
        "day": day.isoformat(),
        "cost_total": sum(by_agent.values()),
        "cost_by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1])),
        "llm_calls": calls,
        "jobs_found": count("SELECT COUNT(*) FROM audit_log WHERE action='job_discovered' AND ts>=? AND ts<?"),
        "proposals": count("SELECT COUNT(*) FROM audit_log WHERE action='transition' AND detail LIKE 'proposed -> awaiting_approval%' AND ts>=? AND ts<?"),
        "bids": count("SELECT COUNT(*) FROM audit_log WHERE action='transition' AND detail LIKE 'awaiting_approval -> submitted%' AND ts>=? AND ts<?"),
        "builds": count("SELECT COUNT(*) FROM audit_log WHERE action='transition' AND detail LIKE '%-> delivered%' AND ts>=? AND ts<?"),
        "month_cost": db.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM audit_log WHERE ts>=? AND ts<?",
                                      (month_start, end)).fetchone()[0],
    }


def format_report(data: dict, balance: float | None, prev_balance: float | None) -> str:
    lines = [f"📊 Daily report — {data['day']}", "",
             f"💵 AI cost today (est.): ${data['cost_total']:.4f}  ({data['llm_calls']} model calls)"]
    for agent, cost in data["cost_by_agent"].items():
        lines.append(f"   • {agent}: ${cost:.4f}")
    lines.append(f"📅 This month so far (est.): ${data['month_cost']:.4f}")
    if balance is not None:
        line = f"🏦 DeepSeek balance: ${balance:.2f}"
        if prev_balance is not None:
            used = prev_balance - balance
            line += f" (used ${used:.2f} since last report)" if used >= 0 else f" (+${-used:.2f}: top-up?)"
        lines += [line, "   (balance is shared with any other project using this key)"]
    lines += ["", f"🔎 Jobs found: {data['jobs_found']}   ✍️ Proposals: {data['proposals']}",
              f"📨 Bids placed: {data['bids']}   📦 Deliveries: {data['builds']}"]
    return "\n".join(lines)


def build_report(db: DB, day: date, deepseek_key: str | None, remember_balance: bool = True) -> str:
    """Report text; also stores today's DeepSeek balance so tomorrow can show the change."""
    balance = deepseek_balance(deepseek_key)
    prev = db.kv_get("deepseek_balance_last")
    text = format_report(collect(db, day), balance, float(prev) if prev else None)
    if remember_balance and balance is not None:
        db.kv_set("deepseek_balance_last", str(balance))
    return text
