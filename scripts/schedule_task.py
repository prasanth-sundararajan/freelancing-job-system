"""Register (or remove) the Windows scheduled tasks.

    .\\.venv\\Scripts\\python.exe scripts\\schedule_task.py              # job search at config schedule.cycle_times
    .\\.venv\\Scripts\\python.exe scripts\\schedule_task.py --remove
    .\\.venv\\Scripts\\python.exe scripts\\schedule_task.py --watchdog   # Ops agent every 15 min
    .\\.venv\\Scripts\\python.exe scripts\\schedule_task.py --bot        # Telegram bot at log-on
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASK = "FreelanceAgents Cycle"          # legacy every-N-hours task (removed when installing times)
TASK_PREFIX = "FreelanceAgents Cycle "   # one task per daily time, e.g. "FreelanceAgents Cycle 1400"

BOT_TASK = "FreelanceAgents Telegram Bot"
WATCHDOG_TASK = "FreelanceAgents Watchdog"


def cycle_times() -> list[str]:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return list(cfg.get("schedule", {}).get("cycle_times") or ["14:00", "18:00", "22:00"])


def cycle_task_names() -> list[str]:
    return [TASK_PREFIX + t.replace(":", "") for t in cycle_times()]


def existing_cycle_tasks() -> list[str]:
    out = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    names = {line.split(",")[0].strip('"').lstrip("\\") for line in out.splitlines() if line}
    return sorted(n for n in names if n == TASK or n.startswith(TASK_PREFIX))


parser = argparse.ArgumentParser()
parser.add_argument("--remove", action="store_true")
parser.add_argument("--bot", action="store_true", help="the Telegram bot task (starts at log-on) instead of the cycle")
parser.add_argument("--watchdog", action="store_true", help="the Ops agent, every 15 minutes")
args = parser.parse_args()

if args.watchdog:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"   # no console window every 15 min
    cmd = (["schtasks", "/Delete", "/TN", WATCHDOG_TASK, "/F"] if args.remove else
           ["schtasks", "/Create", "/SC", "MINUTE", "/MO", "15", "/TN", WATCHDOG_TASK,
            "/TR", f'"{pythonw}" "{ROOT / "scripts" / "watchdog.py"}"', "/F"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    print((result.stdout + result.stderr).strip())
    sys.exit(result.returncode)

if args.bot:
    # A shortcut in the user's Startup folder (log-on tasks need admin rights).
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    link = startup / f"{BOT_TASK}.lnk"
    if args.remove:
        link.unlink(missing_ok=True)
        print(f"removed {link}")
        sys.exit(0)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
          f"$s.TargetPath='{ROOT / '.venv' / 'Scripts' / 'pythonw.exe'}';"
          f"$s.Arguments='\"{ROOT / 'scripts' / 'telegram_bot.py'}\"';"
          f"$s.WorkingDirectory='{ROOT}';$s.Save()")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    print((result.stdout + result.stderr).strip() or f"created {link}")
    sys.exit(result.returncode)

python = ROOT / ".venv" / "Scripts" / "python.exe"
rc = 0
for name in existing_cycle_tasks():             # clear old ones (incl. the legacy every-3-hours task)
    subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
    print(f"removed {name}")
if not args.remove:
    for t in cycle_times():
        name = TASK_PREFIX + t.replace(":", "")
        r = subprocess.run(["schtasks", "/Create", "/SC", "DAILY", "/ST", t, "/TN", name,
                            "/TR", f'"{python}" "{ROOT / "scripts" / "run_cycle.py"}"', "/F"],
                           capture_output=True, text=True)
        print((r.stdout + r.stderr).strip())
        rc = rc or r.returncode
sys.exit(rc)
