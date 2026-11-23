"""SQLite storage: jobs, agent outputs, audit log, bids, approval queue.

SQLite keeps the scaffold free and local. Swap for Postgres when scaling.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    data TEXT NOT NULL,
    state TEXT NOT NULL,
    retries INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    output TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    job_id TEXT,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bids (
    job_id TEXT PRIMARY KEY,           -- unique: a job can never be bid on twice
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    platform_bid_id TEXT,
    credits_spent REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS telegram_notified (
    approval_id INTEGER PRIMARY KEY,
    message_id INTEGER,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS config_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    current REAL,
    suggested REAL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # --- jobs ----------------------------------------------------------------
    def insert_job(self, job: dict) -> bool:
        """Insert a new job in state 'discovered'. Returns False if it already exists."""
        try:
            self._exec(
                "INSERT INTO jobs (job_id, platform, data, state, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (job["job_id"], job["platform"], json.dumps(job), "discovered", now(), now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def jobs_in_state(self, state: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs WHERE state=? ORDER BY created_at", (state,)).fetchall()

    def all_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()

    def set_state(self, job_id: str, state: str) -> None:
        self._exec("UPDATE jobs SET state=?, retries=0, updated_at=? WHERE job_id=?", (state, now(), job_id))

    def requeue_model_rejections(self) -> int:
        """Send jobs the Qualifier MODEL rejected back to 'discovered' (e.g. after a prompt fix).

        Jobs rejected by code guardrails (prescreen) stay rejected.
        """
        rows = self.conn.execute(
            "SELECT j.job_id FROM jobs j WHERE j.state='rejected' AND EXISTS ("
            " SELECT 1 FROM audit_log a WHERE a.job_id=j.job_id AND a.agent='qualifier' AND a.action='llm_call')"
            " AND NOT EXISTS (SELECT 1 FROM agent_outputs o WHERE o.job_id=j.job_id AND o.agent='estimator')"
        ).fetchall()
        for (job_id,) in rows:
            self._exec("UPDATE jobs SET state='discovered', retries=0, updated_at=? WHERE job_id=?", (now(), job_id))
            self.log("orchestrator", "requeued", job_id, "re-qualify after Qualifier change")
        return len(rows)

    def reset_queued(self, refreshed: dict[str, dict] | None = None) -> list[str]:
        """Pull every job awaiting approval back to 'discovered' so it is re-screened and re-priced.

        Removes its pending approval and queued bid. `refreshed` (job_id -> Job dict)
        replaces the stored job data, e.g. fresh data from the platform.
        """
        refreshed = refreshed or {}
        ids = [r[0] for r in self.conn.execute("SELECT job_id FROM jobs WHERE state='awaiting_approval'")]
        for job_id in ids:
            self._exec("DELETE FROM approval_queue WHERE job_id=? AND status='pending'", (job_id,))
            self._exec("DELETE FROM bids WHERE job_id=? AND status='queued'", (job_id,))
            if job_id in refreshed:
                self._exec("UPDATE jobs SET data=? WHERE job_id=?", (json.dumps(refreshed[job_id]), job_id))
            self._exec("UPDATE jobs SET state='discovered', retries=0, updated_at=? WHERE job_id=?", (now(), job_id))
            self.log("orchestrator", "reset_queued", job_id, "re-screen and re-price")
        return ids

    def undo_submitted(self, job_id: str) -> None:
        """A bid was recorded as submitted but wasn't placed: back to the approval queue."""
        self._exec("UPDATE jobs SET state='awaiting_approval', updated_at=? WHERE job_id=? AND state='submitted'", (now(), job_id))
        self._exec("UPDATE approval_queue SET status='pending', decided_at=NULL WHERE job_id=? AND kind='proposal' "
                   "AND id=(SELECT MAX(id) FROM approval_queue WHERE job_id=? AND kind='proposal')", (job_id, job_id))
        self._exec("UPDATE bids SET status='queued' WHERE job_id=?", (job_id,))
        self._exec("DELETE FROM telegram_notified WHERE approval_id IN (SELECT id FROM approval_queue WHERE job_id=?)", (job_id,))
        self.log("orchestrator", "undo_submitted", job_id, "bid not found on platform; back to awaiting_approval")

    def bump_retries(self, job_id: str) -> int:
        self._exec("UPDATE jobs SET retries=retries+1, updated_at=? WHERE job_id=?", (now(), job_id))
        return self.get_job(job_id)["retries"]

    def add_cost(self, job_id: str, cost: float) -> None:
        if cost:
            self._exec("UPDATE jobs SET cost_usd=cost_usd+? WHERE job_id=?", (cost, job_id))

    # --- outputs & audit -----------------------------------------------------
    def save_output(self, job_id: str, agent: str, output: dict) -> None:
        self._exec(
            "INSERT INTO agent_outputs (job_id, agent, output, created_at) VALUES (?,?,?,?)",
            (job_id, agent, json.dumps(output), now()),
        )

    def latest_output(self, job_id: str, agent: str) -> dict | None:
        row = self.conn.execute(
            "SELECT output FROM agent_outputs WHERE job_id=? AND agent=? ORDER BY id DESC LIMIT 1",
            (job_id, agent),
        ).fetchone()
        return json.loads(row["output"]) if row else None

    def outputs(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM agent_outputs WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    def log(self, agent: str, action: str, job_id: str | None = None, detail: str | None = None, cost: float = 0.0) -> None:
        self._exec(
            "INSERT INTO audit_log (ts, job_id, agent, action, detail, cost_usd) VALUES (?,?,?,?,?,?)",
            (now(), job_id, agent, action, detail, cost),
        )

    # --- bids & approvals ----------------------------------------------------
    def bids_today(self) -> int:
        today = now()[:10]
        return self.conn.execute("SELECT COUNT(*) FROM bids WHERE substr(created_at,1,10)=?", (today,)).fetchone()[0]

    def has_bid(self, job_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM bids WHERE job_id=?", (job_id,)).fetchone() is not None

    def add_bid(self, job_id: str, channel: str, status: str, platform_bid_id: str | None, credits: float) -> None:
        self._exec(
            "INSERT INTO bids (job_id, channel, status, platform_bid_id, credits_spent, created_at) VALUES (?,?,?,?,?,?)",
            (job_id, channel, status, platform_bid_id, credits, now()),
        )

    def set_bid_status(self, job_id: str, status: str, platform_bid_id: str | None = None) -> None:
        if platform_bid_id is None:
            self._exec("UPDATE bids SET status=? WHERE job_id=?", (status, job_id))
        else:
            self._exec("UPDATE bids SET status=?, platform_bid_id=? WHERE job_id=?", (status, platform_bid_id, job_id))

    def enqueue_approval(self, job_id: str, kind: str, payload: dict) -> None:
        self._exec(
            "INSERT INTO approval_queue (job_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (job_id, kind, json.dumps(payload), now()),
        )

    def pending_approvals(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.conn.execute(
                "SELECT * FROM approval_queue WHERE status='pending' AND kind=? ORDER BY id", (kind,)).fetchall()
        return self.conn.execute("SELECT * FROM approval_queue WHERE status='pending' ORDER BY id").fetchall()

    def get_approval(self, approval_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approval_queue WHERE id=?", (approval_id,)).fetchone()

    def decide_approval_id(self, approval_id: int, status: str) -> None:
        self._exec("UPDATE approval_queue SET status=?, decided_at=? WHERE id=?", (status, now(), approval_id))

    def update_approval_payload(self, approval_id: int, payload: dict) -> None:
        self._exec("UPDATE approval_queue SET payload=? WHERE id=?", (json.dumps(payload), approval_id))

    # --- telegram / key-value ------------------------------------------------
    def tg_notified(self, approval_id: int) -> bool:
        return self.conn.execute("SELECT 1 FROM telegram_notified WHERE approval_id=?", (approval_id,)).fetchone() is not None

    def tg_mark(self, approval_id: int, message_id: int | None) -> None:
        self._exec("INSERT OR REPLACE INTO telegram_notified (approval_id, message_id, sent_at) VALUES (?,?,?)",
                   (approval_id, message_id, now()))

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str | None) -> None:
        if value is None:
            self._exec("DELETE FROM kv WHERE key=?", (key,))
        else:
            self._exec("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, value))

    def escalations_since(self, ts: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT job_id, ts, detail FROM audit_log WHERE action='transition' AND detail LIKE '%-> escalated%' AND ts>? ORDER BY ts",
            (ts,)).fetchall()

    def reply_drafted(self, job_id: str, message_id) -> bool:
        """True if a reply draft already exists for this client message."""
        for row in self.conn.execute("SELECT payload FROM approval_queue WHERE job_id=? AND kind='reply'", (job_id,)):
            if json.loads(row[0]).get("message_id") == message_id:
                return True
        return False

    def decide_approval(self, job_id: str, status: str) -> bool:
        cur = self._exec(
            "UPDATE approval_queue SET status=?, decided_at=? WHERE job_id=? AND status='pending'",
            (status, now(), job_id),
        )
        return cur.rowcount > 0

    def add_config_suggestion(self, key: str, current: float, suggested: float, reason: str) -> None:
        self._exec(
            "INSERT INTO config_suggestions (key, current, suggested, reason, created_at) VALUES (?,?,?,?,?)",
            (key, current, suggested, reason, now()),
        )
