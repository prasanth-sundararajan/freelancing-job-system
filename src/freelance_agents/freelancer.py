"""Freelancer.com official API client (https://developers.freelancer.com).

Allowed use here:
- Searching active projects ("Work Sourcer" integration).
- Placing ONE bid at a time after a human approves it (`approve --submit`).

Freelancer's API terms prohibit automatic bidders, so nothing in this module
bids on its own. Bid submission is also off unless `freelancer.bid_via_api`
is true in config. Check with api-support@freelancer.com before enabling it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from .guardrails import exclude_pattern

BASE_URLS = {
    "production": "https://www.freelancer.com/api",
    "sandbox": "https://www.freelancer-sandbox.com/api",
}

# Projections that add client details to search results.
SEARCH_PROJECTIONS = (
    "full_description",
    "job_details",
    "user_details",
    "user_status",
    "user_employer_reputation",
    "user_country_details",
)


class FreelancerAPIError(Exception):
    pass


class RateLimited(FreelancerAPIError):
    pass


class FreelancerAPI:
    def __init__(self, token: str, environment: str = "production", http: httpx.Client | None = None):
        if not token:
            raise FreelancerAPIError("FREELANCER_OAUTH_TOKEN is not set")
        if environment not in BASE_URLS:
            raise FreelancerAPIError(f"unknown environment '{environment}'")
        self.base_url = BASE_URLS[environment]
        self.environment = environment
        self.http = http or httpx.Client(timeout=30)
        self.headers = {"freelancer-oauth-v1": token}

    def _handle(self, resp: httpx.Response) -> dict:
        if resp.status_code == 429:
            raise RateLimited("Freelancer API rate limit hit; try again later")
        try:
            body = resp.json()
        except ValueError:
            raise FreelancerAPIError(f"HTTP {resp.status_code}: non-JSON response")
        if resp.status_code >= 400 or body.get("status") != "success":
            raise FreelancerAPIError(f"HTTP {resp.status_code}: {body.get('message') or body.get('error_code') or 'error'}")
        return body.get("result") or {}

    def get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict:
        return self._handle(self.http.get(f"{self.base_url}{path}", params=params, headers=self.headers))

    def post(self, path: str, payload: dict) -> dict:
        return self._handle(self.http.post(f"{self.base_url}{path}", json=payload, headers=self.headers))

    # --- endpoints -----------------------------------------------------------
    def self_id(self) -> int:
        return int(self.get("/users/0.1/self/")["id"])

    def resolve_job_ids(self, names: list[str]) -> dict[str, int]:
        """Skill ("job") names -> Freelancer job IDs, e.g. {"Python": 13}."""
        if not names:
            return {}
        result = self.get("/projects/0.1/jobs/", [("job_names[]", n) for n in names])
        items = result if isinstance(result, list) else result.get("jobs", [])
        return {j["name"]: int(j["id"]) for j in items if j.get("name") in names}

    def search_active(self, query: str | None = None, *, job_ids=(), project_types=("fixed",), min_price=None, max_price=None, limit=50) -> dict:
        params: list[tuple[str, str]] = [("limit", str(limit)), ("sort_field", "time_updated"), ("compact", "true")]
        if query:
            params.append(("query", query))
        params += [("jobs[]", str(i)) for i in job_ids]
        params += [("project_types[]", t) for t in project_types]
        if min_price is not None:
            params.append(("min_price", str(min_price)))
        if max_price is not None:
            params.append(("max_price", str(max_price)))
        params += [(p, "true") for p in SEARCH_PROJECTIONS]
        return self.get("/projects/0.1/projects/active/", params)

    def my_bids(self, project_id: int) -> list[dict]:
        """Bids the token's user has placed on one project (empty if none)."""
        result = self.get("/projects/0.1/bids/", [("projects[]", str(project_id)), ("bidders[]", str(self.self_id()))])
        return result.get("bids") or []

    # --- messaging (scope fln:messaging) -------------------------------------
    def list_threads(self, limit: int = 50) -> list[dict]:
        """Project threads, newest first. Each item: {"id", "thread": {"context": {"type", "id"}, ...}, ...}."""
        # context_type can only be sent together with contexts[]; filter project threads in code instead.
        result = self.get("/messages/0.1/threads/", [("last_message", "true"), ("unread_count", "true"),
                                                     ("limit", str(limit))])
        return result.get("threads") or []

    def list_messages(self, thread_id: int, limit: int = 50) -> list[dict]:
        result = self.get("/messages/0.1/messages/", [("threads[]", str(thread_id)), ("limit", str(limit))])
        msgs = result.get("messages") or []
        return sorted(msgs, key=lambda m: m.get("time_created") or 0)

    def send_message(self, thread_id: int, text: str) -> dict:
        return self._handle(self.http.post(f"{self.base_url}/messages/0.1/threads/{thread_id}/messages/",
                                           data={"message": text}, headers=self.headers))

    def projects_by_id(self, ids: list[int]) -> list[dict]:
        """Fresh project data (as Job dicts) for the given Freelancer project IDs."""
        if not ids:
            return []
        params = [("projects[]", str(i)) for i in ids] + [(p, "true") for p in SEARCH_PROJECTIONS]
        result = self.get("/projects/0.1/projects/", params)
        return [map_project(p, result.get("users")) for p in result.get("projects") or []]

    def place_bid(self, *, project_id: int, bidder_id: int, amount: float, period: int, description: str, milestone_percentage: int = 100) -> dict:
        return self.post(
            "/projects/0.1/bids/",
            {
                "project_id": project_id,
                "bidder_id": bidder_id,
                "amount": amount,
                "period": period,
                "milestone_percentage": milestone_percentage,
                "description": description,
            },
        )


def _user_for(users, owner_id) -> dict:
    """`result.users` may be keyed by id (string) or be a list."""
    if isinstance(users, dict):
        return users.get(str(owner_id)) or users.get(owner_id) or {}
    if isinstance(users, list):
        return next((u for u in users if u.get("id") == owner_id), {})
    return {}


def map_project(project: dict, users) -> dict:
    """Map a Freelancer project (+ owner) to our Job schema (as a dict)."""
    owner = _user_for(users, project.get("owner_id"))
    history = ((owner.get("employer_reputation") or {}).get("entire_history")) or {}
    country = (((owner.get("location") or {}).get("country")) or {}).get("code")
    budget = project.get("budget") or {}
    ts = project.get("time_submitted") or project.get("time_updated")
    posted = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None
    seo = project.get("seo_url")
    return {
        "job_id": f"freelancer:{project['id']}",
        "platform": "freelancer",
        "url": f"https://www.freelancer.com/projects/{seo}" if seo else f"https://www.freelancer.com/projects/{project['id']}",
        "title": project.get("title", ""),
        "description": project.get("description") or project.get("preview_description") or "",
        "skills": [j.get("name") for j in (project.get("jobs") or []) if j.get("name")],
        "budget": {
            "type": project.get("type") or budget.get("project_type") or "fixed",
            "min": budget.get("minimum") or 0,
            "max": budget.get("maximum") or 0,
            "currency": ((project.get("currency") or {}).get("code")) or "USD",
            "usd_rate": ((project.get("currency") or {}).get("exchange_rate")) or 1.0,
        },
        "competition": {
            "bid_count": (project.get("bid_stats") or {}).get("bid_count"),
            "bid_avg": (project.get("bid_stats") or {}).get("bid_avg"),
        },
        "deadline": None,
        "client": {
            "country": country.upper() if country else None,
            "rating": history.get("overall"),
            "hires": history.get("complete"),
            "spent": None,  # needs the user:financial scope
            "payment_verified": (owner.get("status") or {}).get("payment_verified"),
        },
        "posted_at": posted or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "api",
    }


class FreelancerAPISource:
    """Scout source: one search per skill group (and per optional keyword query).

    Searching by Freelancer skill IDs returns far fewer off-niche projects than
    keyword search. `exclude_keywords` then drops obvious mismatches in code,
    before any model call.
    """

    name = "freelancer_api"

    def __init__(self, api: FreelancerAPI, search_cfg: dict, exclude_keywords: list[str] | None = None):
        self.api = api
        self.search_cfg = search_cfg
        self.exclude_keywords = exclude_keywords if exclude_keywords is not None else search_cfg.get("exclude_keywords", [])
        self.stats: dict = {}

    def searches(self) -> list[tuple[str, str | None, list[int]]]:
        groups: dict[str, list[str]] = self.search_cfg.get("skill_groups") or {}
        names = sorted({n for group in groups.values() for n in group})
        ids = self.api.resolve_job_ids(names)
        missing = [n for n in names if n not in ids]
        if missing:
            self.stats["unknown_skills"] = missing
        out = [(f"skills:{g}", None, [ids[n] for n in group if n in ids]) for g, group in groups.items()]
        out = [s for s in out if s[2]]
        out += [(f"query:{q}", q, []) for q in self.search_cfg.get("queries") or []]
        return out

    def fetch(self) -> list[dict]:
        cfg = self.search_cfg
        excluded = exclude_pattern(self.exclude_keywords or [])
        stats = {"fetched": 0, "skipped_not_open": 0, "skipped_nda_or_preferred": 0, "skipped_excluded_keyword": 0, "per_search": {}}
        self.stats.update(stats)
        seen: dict[str, dict] = {}
        for label, query, job_ids in self.searches():
            result = self.api.search_active(
                query,
                job_ids=job_ids,
                project_types=cfg.get("project_types", ["fixed"]),
                min_price=cfg.get("min_price"),
                max_price=cfg.get("max_price"),
                limit=cfg.get("limit_per_query", 30),
            )
            users = result.get("users")
            projects = result.get("projects") or []
            self.stats["fetched"] += len(projects)
            kept = 0
            for project in projects:
                upgrades = project.get("upgrades") or {}
                if project.get("status") not in (None, "active"):
                    self.stats["skipped_not_open"] += 1
                    continue
                if upgrades.get("NDA") or upgrades.get("pf_only"):
                    self.stats["skipped_nda_or_preferred"] += 1  # we can't sign NDAs or claim preferred status
                    continue
                text = f"{project.get('title', '')}\n{project.get('description') or project.get('preview_description') or ''}"
                if excluded and excluded.search(text):
                    self.stats["skipped_excluded_keyword"] += 1
                    continue
                job = map_project(project, users)
                if job["job_id"] not in seen:
                    seen[job["job_id"]] = job
                    kept += 1
            self.stats["per_search"][label] = kept
        return list(seen.values())
