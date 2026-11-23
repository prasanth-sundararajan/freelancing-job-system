"""Sign in to Freelancer once in the bot's own Chrome profile.

    .\\.venv\\Scripts\\python.exe scripts\\browser_signin.py

Opens (or reuses) the bot's Chrome on the Freelancer login page; sign in there
(password and any codes are yours to enter). That Chrome keeps running after
this script ends, so the sign-in stays while the window is open.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from freelance_agents.bid_filler import BidFiller  # noqa: E402

filler = BidFiller(ROOT / "data" / "browser_profile")
print("A Chrome window is opening on the Freelancer login page. Sign in there (you have 10 minutes).")
try:
    ok = filler.sign_in(timeout_s=600)
finally:
    filler.close()
print("Signed in. Leave that Chrome window open; the bot uses it." if ok else "Not signed in (timed out). Run this again when ready.")
sys.exit(0 if ok else 1)
