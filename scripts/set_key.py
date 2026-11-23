"""Save an API key to .env without showing it on screen.

    .\\.venv\\Scripts\\python.exe scripts\\set_key.py DEEPSEEK_API_KEY
"""
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = sys.argv[1] if len(sys.argv) > 1 else "DEEPSEEK_API_KEY"

key = getpass.getpass(f"Paste your {NAME} (it won't show), then press Enter: ").strip().strip("'\"")
if not key:
    print("No key entered; nothing changed.")
    sys.exit(1)

env = ROOT / ".env"
lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
lines = [line for line in lines if not line.startswith(f"{NAME}=")] + [f"{NAME}={key}"]
env.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Saved {NAME} to {env} ({len(key)} characters). You can close this tab.")
