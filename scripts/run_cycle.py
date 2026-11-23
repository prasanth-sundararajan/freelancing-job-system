"""One automatic cycle: find jobs, qualify/price/write proposals, draft client replies, sync bids.

Run by Windows Task Scheduler every 3 hours (see scripts/schedule_task.py), and by the
Ops agent when a run was missed. Proposals go to Telegram for your tap.
Output is appended to data/logs/cycle_YYYY-MM-DD.log.
"""
import contextlib
import io
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

from freelance_agents.cli import main  # noqa: E402

log_dir = ROOT / "data" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = log_dir / f"cycle_{datetime.now():%Y-%m-%d}.log"

# One cycle at a time (the scheduled task and the Ops agent's re-run must never overlap).
lock = ROOT / "data" / "cycle.lock"
if lock.exists() and (datetime.now().timestamp() - lock.stat().st_mtime) < 60 * 60:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} ===== skipped: another cycle is running\n")
    sys.exit(0)
lock.write_text(str(os.getpid()), encoding="utf-8")

buf = io.StringIO()
codes = {}
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
    for step in (["scout", "--freelancer"], ["run"], ["inbox"], ["sync-bids"]):
        try:
            codes[step[0]] = main(step)
        except SystemExit as e:  # argparse errors
            codes[step[0]] = e.code
        except Exception as e:  # keep the schedule alive; log and continue
            print(f"{step[0]} failed: {type(e).__name__}: {e}")
            codes[step[0]] = 1

lock.unlink(missing_ok=True)
with open(log_file, "a", encoding="utf-8") as f:
    f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n{buf.getvalue()}exit codes: {codes}\n")
sys.exit(0 if all(c in (0, None) for c in codes.values()) else 1)
