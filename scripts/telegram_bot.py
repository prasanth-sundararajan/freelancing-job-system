"""Run the Telegram approval bot (long-running).

    .\\.venv\\Scripts\\python.exe scripts\\telegram_bot.py

Needs TELEGRAM_BOT_TOKEN in .env (from @BotFather). The first time, it prints a
pairing code: send `/start <code>` to your bot from your own Telegram account.
Your user ID is then saved as TELEGRAM_OWNER_ID and only you can use the bot.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

if sys.stdout is None:  # started windowless (pythonw from the Startup folder): log to a file
    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(ROOT / "data" / "logs" / "telegram_bot.log", "a", encoding="utf-8", buffering=1)

from freelance_agents.cli import build_ctx, freelancer_api  # noqa: E402
from freelance_agents.telegram_bot import ApprovalBot, TelegramClient  # noqa: E402


def save_owner(owner_id: int) -> None:
    env = ROOT / ".env"
    lines = [l for l in env.read_text(encoding="utf-8").splitlines() if not l.startswith("TELEGRAM_OWNER_ID=")]
    env.write_text("\n".join(lines + [f"TELEGRAM_OWNER_ID={owner_id}"]) + "\n", encoding="utf-8")
    print(f"Paired with Telegram user {owner_id} (saved to .env)")


token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    sys.exit("TELEGRAM_BOT_TOKEN is not set. Run: .\\.venv\\Scripts\\python.exe scripts\\set_key.py TELEGRAM_BOT_TOKEN")
owner = os.getenv("TELEGRAM_OWNER_ID")
ctx = build_ctx()
fill_cfg = ctx.cfg.get("freelancer", {}).get("bid_fill", {})
bid_filler = None
if fill_cfg.get("enabled"):
    from freelance_agents.bid_filler import BidFiller
    bid_filler = BidFiller(ROOT / fill_cfg.get("browser_profile", "data/browser_profile"))
bot = ApprovalBot(ctx, TelegramClient(token), int(owner) if owner else None,
                  api_factory=lambda: freelancer_api(ctx.cfg), ctx_factory=build_ctx, on_pair=save_owner,
                  bid_filler=bid_filler)
if bot.pair_code:
    print(f"Not paired yet. In Telegram, open your bot and send:  /start {bot.pair_code}")
else:
    print(f"Bot running for Telegram user {owner}. Ctrl+C to stop.")
bot.run_forever()
