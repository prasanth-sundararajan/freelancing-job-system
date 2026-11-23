"""Back up and replace your Freelancer.com skills via the official API.

    .\\.venv\\Scripts\\python.exe scripts\\update_profile_skills.py            # apply the list below
    .\\.venv\\Scripts\\python.exe scripts\\update_profile_skills.py --restore profile\\backup_....json

Uses PUT /users/0.1/self/jobs (sets the skill list). A backup of the current
skills, tagline and description is saved to profile/ first.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from freelance_agents.freelancer import FreelancerAPI  # noqa: E402

SKILLS = [
    "Web Scraping", "Data Scraping", "Scrapy", "Web Crawling",
    "Python", "Automation",
    "Excel", "Data Processing", "Data Cleansing", "Data Entry",
    "HTML", "CSS", "Landing Pages",
    "SEO Writing", "Article Writing", "Technical Writing",
]

parser = argparse.ArgumentParser()
parser.add_argument("--restore", help="backup JSON to restore skills from")
args = parser.parse_args()

api = FreelancerAPI(os.environ["FREELANCER_OAUTH_TOKEN"], "production")


def current() -> dict:
    me = api.get("/users/0.1/self/", [("jobs", "true"), ("profile_description", "true")])
    return {
        "tagline": me.get("tagline"),
        "profile_description": me.get("profile_description"),
        "hourly_rate": me.get("hourly_rate"),
        "skills": [{"id": j["id"], "name": j["name"]} for j in (me.get("jobs") or [])],
    }


before = current()
if args.restore:
    ids = [s["id"] for s in json.loads(Path(args.restore).read_text(encoding="utf-8"))["skills"]]
else:
    backup = ROOT / "profile" / f"backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps(before, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"backup saved: {backup}")
    resolved = api.resolve_job_ids(SKILLS)
    missing = [s for s in SKILLS if s not in resolved]
    if missing:
        print(f"not found on Freelancer, skipped: {missing}")
    ids = [resolved[s] for s in SKILLS if s in resolved]

print("before:", [s["name"] for s in before["skills"]])
api._handle(api.http.put(f"{api.base_url}/users/0.1/self/jobs", json={"jobs[]": ids}, headers=api.headers))
after = current()
print("after: ", [s["name"] for s in after["skills"]])
wanted = set(ids)
got = {s["id"] for s in after["skills"]}
if got != wanted:
    print(f"WARNING: requested {len(wanted)} skills, profile now has {len(got)}. Missing: {sorted(wanted - got)}")
