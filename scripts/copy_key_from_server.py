"""Copy the DeepSeek key from a server's .env into this project's .env, without showing it.

    .\\.venv\\Scripts\\python.exe scripts\\copy_key_from_server.py root@SERVER_IP
    .\\.venv\\Scripts\\python.exe scripts\\copy_key_from_server.py root@SERVER_IP --identity C:\\Users\\HP\\.ssh\\yt_factory --remote-env /opt/ytfactory/.env
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "DEEPSEEK_API_KEY"

parser = argparse.ArgumentParser()
parser.add_argument("target", help="user@host of the server")
parser.add_argument("--identity", default=str(Path.home() / ".ssh" / "yt_factory"))
parser.add_argument("--remote-env", default="/opt/ytfactory/.env")
args = parser.parse_args()

result = subprocess.run(
    ["ssh", "-i", args.identity, args.target, f"grep -E '^[A-Za-z_]*DEEPSEEK[A-Za-z_]*=' {args.remote_env}"],
    capture_output=True, text=True,
)
if result.returncode != 0 or not result.stdout.strip():
    print(f"No DEEPSEEK key found in {args.remote_env} on {args.target}.")
    print(result.stderr.strip())
    sys.exit(1)

lines = result.stdout.strip().splitlines()
line = next((l for l in lines if l.startswith(f"{NAME}=")), lines[0])
remote_name, value = line.split("=", 1)
key = value.strip().strip("'\"")
if not re.fullmatch(r"\S{10,}", key):
    print(f"Found {remote_name} but the value looks empty or malformed; nothing changed.")
    sys.exit(1)

env = ROOT / ".env"
existing = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
existing = [l for l in existing if not l.startswith(f"{NAME}=")] + [f"{NAME}={key}"]
env.write_text("\n".join(existing) + "\n", encoding="utf-8")
print(f"Copied {remote_name} from {args.target} into {env} as {NAME} ({len(key)} characters).")
