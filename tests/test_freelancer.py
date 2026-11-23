import json

import httpx
import pytest

from conftest import est, proposal, qual
from freelance_agents.agents import ScoutAgent
from freelance_agents.freelancer import (
    FreelancerAPI,
    FreelancerAPIError,
    FreelancerAPISource,
    RateLimited,
    map_project,
)
from freelance_agents.orchestrator import Orchestrator

NOW = 1_900_000_000  # far-future timestamp so the job is never "too old"


def project(pid, title="Python script to scrape prices", **extra):
    p = {
        "id": pid, "owner_id": 77, "title": title, "status": "active", "type": "fixed",
        "seo_url": f"python/scrape-{pid}", "description": "Scrape 3 public sites into Excel.",
        "preview_description": "Scrape 3...", "currency": {"id": 1, "code": "USD", "sign": "$"},
        "budget": {"minimum": 100, "maximum": 200, "currency_id": 1},
        "jobs": [{"id": 13, "name": "Python"}, {"id": 95, "name": "Web Scraping"}],
        "time_submitted": NOW, "upgrades": {"NDA": False, "pf_only": False},
    }
    p.update(extra)
    return p


USERS = {"77": {
    "id": 77,
    "status": {"payment_verified": True},
    "employer_reputation": {"entire_history": {"overall": 4.8, "complete": 9, "reviews": 7}},
    "location": {"country": {"code": "us", "name": "United States"}},
}}


def make_api(handler, environment="production"):
    return FreelancerAPI("tok", environment, http=httpx.Client(transport=httpx.MockTransport(handler)))


def ok(result):
    return httpx.Response(200, json={"status": "success", "result": result, "request_id": "x"})


def test_search_sends_auth_and_params():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        seen["auth"] = request.headers.get("freelancer-oauth-v1")
        return ok({"projects": [project(1)], "users": USERS, "total_count": 1})

    api = make_api(handler, "sandbox")
    api.search_active("python script", project_types=["fixed"], min_price=50, max_price=1000, limit=30)
    url = seen["url"]
    assert str(url).startswith("https://www.freelancer-sandbox.com/api/projects/0.1/projects/active/")
    assert seen["auth"] == "tok"
    q = url.params
    assert q["query"] == "python script"
    assert q.get_list("project_types[]") == ["fixed"]
    assert q["min_price"] == "50" and q["max_price"] == "1000" and q["limit"] == "30"
    for projection in ("full_description", "job_details", "user_details", "user_status", "user_employer_reputation"):
        assert projection in q


def test_map_project_to_job():
    job = map_project(project(42), USERS)
    assert job["job_id"] == "freelancer:42"
    assert job["url"] == "https://www.freelancer.com/projects/python/scrape-42"
    assert job["skills"] == ["Python", "Web Scraping"]
    assert job["budget"] == {"type": "fixed", "min": 100, "max": 200, "currency": "USD", "usd_rate": 1.0}
    assert job["client"] == {"country": "US", "rating": 4.8, "hires": 9, "spent": None, "payment_verified": True}
    inr = map_project(project(43, currency={"code": "INR", "exchange_rate": 0.01079}, bid_stats={"bid_count": 20, "bid_avg": 7236.05}), USERS)
    assert inr["budget"]["currency"] == "INR" and inr["budget"]["usd_rate"] == 0.01079
    assert inr["competition"] == {"bid_count": 20, "bid_avg": 7236.05}
    assert job["description"] == "Scrape 3 public sites into Excel."


def test_source_dedupes_and_skips_nda(make_ctx):
    def handler(request):
        return ok({"projects": [project(1), project(2, upgrades={"NDA": True}), project(3, status="closed")], "users": USERS})

    source = FreelancerAPISource(make_api(handler), {"queries": ["a", "b"], "project_types": ["fixed"]})
    records = source.fetch()
    assert [r["job_id"] for r in records] == ["freelancer:1"]
    ctx = make_ctx({})
    assert ScoutAgent(ctx).ingest("freelancer_api", records)["new"] == 1


def test_skill_group_search_and_exclude_filter():
    searches = []

    def handler(request):
        if request.url.path.endswith("/projects/0.1/jobs/"):
            assert request.url.params.get_list("job_names[]") == ["Data Scraping", "Python", "Web Scraping"]
            return ok([{"id": 13, "name": "Python"}, {"id": 95, "name": "Web Scraping"}])  # "Data Scraping" unknown
        searches.append(request.url.params)
        return ok({"projects": [
            project(1),
            project(2, title="Explainer video for Python course"),          # excluded keyword
            project(3, title="Scrape prices", description="Then make a VoiceOver"),
        ], "users": {}})

    cfg = {
        "skill_groups": {"scraping": ["Web Scraping", "Data Scraping"], "python": ["Python"]},
        "queries": ["price tracker"],
        "exclude_keywords": ["video", "voiceover"],
        "project_types": ["fixed"],
    }
    source = FreelancerAPISource(make_api(handler), cfg)
    records = source.fetch()

    assert [r["job_id"] for r in records] == ["freelancer:1"]
    assert [p.get_list("jobs[]") for p in searches] == [["95"], ["13"], []]
    assert "query" not in searches[0] and searches[2]["query"] == "price tracker"
    assert source.stats["unknown_skills"] == ["Data Scraping"]
    assert source.stats["skipped_excluded_keyword"] == 6  # 2 per search x 3 searches
    assert source.stats["per_search"] == {"skills:scraping": 1, "skills:python": 0, "query:price tracker": 0}


def test_rate_limit_and_errors():
    with pytest.raises(RateLimited):
        make_api(lambda r: httpx.Response(429, json={"status": "error"})).self_id()
    with pytest.raises(FreelancerAPIError):
        make_api(lambda r: httpx.Response(403, json={"status": "error", "message": "forbidden"})).self_id()
    with pytest.raises(FreelancerAPIError):
        FreelancerAPI("", "production")


def queued_freelancer_job(make_ctx, **overrides):
    responses = {
        "qualifier": lambda jid: qual(jid),
        "estimator": lambda jid: est(jid, 150),
        "proposal_writer": lambda jid: proposal(jid, 150),
    }
    ctx = make_ctx(responses, **overrides)
    ScoutAgent(ctx).ingest("freelancer_api", [map_project(project(88231), USERS)])
    orch = Orchestrator(ctx)
    orch.tick()
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"
    return ctx, orch


def test_reset_queued_refreshes_and_requeues(make_ctx):
    ctx, _ = queued_freelancer_job(make_ctx)
    fresh = map_project(project(88231, currency={"code": "INR", "exchange_rate": 0.01079}), USERS)
    assert ctx.db.reset_queued({"freelancer:88231": fresh}) == ["freelancer:88231"]
    row = ctx.db.get_job("freelancer:88231")
    assert row["state"] == "discovered"
    assert json.loads(row["data"])["budget"]["currency"] == "INR"
    assert ctx.db.pending_approvals() == [] and not ctx.db.has_bid("freelancer:88231")


def test_submit_refused_when_bid_via_api_off(make_ctx):
    ctx, orch = queued_freelancer_job(make_ctx)
    with pytest.raises(ValueError, match="bid_via_api"):
        orch.approve("freelancer:88231", submit=True)
    assert ctx.db.get_job("freelancer:88231")["state"] == "awaiting_approval"


def test_submit_dry_run_sends_nothing(make_ctx, cfg):
    ctx, orch = queued_freelancer_job(make_ctx, freelancer={**cfg["freelancer"], "bid_via_api": True})
    message = orch.approve("freelancer:88231", submit=True, api=None)
    assert "NOT sent" in message
    assert ctx.db.get_job("freelancer:88231")["state"] == "submitted"


def test_submit_live_posts_one_bid(make_ctx, cfg):
    calls = []

    def handler(request):
        if request.url.path.endswith("/users/0.1/self/"):
            return ok({"id": 555})
        if request.method == "GET" and request.url.path.endswith("/projects/0.1/bids/"):
            return ok({"bids": [{"id": 9001}] if calls else []})
        calls.append(json.loads(request.content))
        return ok({"id": 9001})

    ctx, orch = queued_freelancer_job(make_ctx, dry_run=False, freelancer={**cfg["freelancer"], "bid_via_api": True})
    orch.approve("freelancer:88231", submit=True, api=make_api(handler))
    assert len(calls) == 1
    body = calls[0]
    assert body["project_id"] == 88231 and body["bidder_id"] == 555
    assert body["amount"] == 150 and body["period"] == 2 and body["milestone_percentage"] == 100
    assert "AI-assisted" in body["description"]
    bid = ctx.db.conn.execute("select * from bids where job_id='freelancer:88231'").fetchone()
    assert bid["status"] == "submitted" and bid["platform_bid_id"] == "9001"
