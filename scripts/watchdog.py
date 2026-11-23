"""Ops agent entry point: run by the 'FreelanceAgents Watchdog' scheduled task every 15 minutes.

    .\\.venv\\Scripts\\python.exe scripts\\watchdog.py
"""
import contextlib
import io
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

from freelance_agents.cli import build_ctx  # noqa: E402
from freelance_agents.telegram_bot import TelegramClient  # noqa: E402
from freelance_agents.watchdog import Watchdog, WindowsSystem  # noqa: E402

notify = None
if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_OWNER_ID"):
    tg, owner = TelegramClient(os.environ["TELEGRAM_BOT_TOKEN"]), int(os.environ["TELEGRAM_OWNER_ID"])
    notify = lambda text: tg.send(owner, text)  # noqa: E731

ctx = build_ctx()
buf = io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    report = Watchdog(ctx, WindowsSystem(ctx.cfg), notify=notify).run()

log = ROOT / "data" / "logs" / "watchdog.log"
log.parent.mkdir(parents=True, exist_ok=True)
with open(log, "a", encoding="utf-8") as f:
    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} fixed={report.fixed} alerts={len(report.alerts)} "
            f"notes={report.notes}{(' ' + buf.getvalue().strip()) if buf.getvalue().strip() else ''}\n")
if sys.stdout:
    print(f"fixed: {report.fixed}\nalerts: {report.alerts}\nnotes: {report.notes}")
