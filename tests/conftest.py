import json
import re

import pytest

from freelance_agents.agents import Ctx
from freelance_agents.config import ROOT, load_config
from freelance_agents.db import DB
from freelance_agents.schemas import (
    BidPrice,
    EstimateTask,
    EstimatorResult,
    ProposalPrice,
    ProposalResult,
    QualifierBreakdown,
    QualifierResult,
)
from freelance_agents.skills_loader import Skills
from freelance_agents.sources import FileSource

PORTFOLIO_URL = "https://your-portfolio.example/price-tracker"


class FakeLLM:
    """Stands in for the real model. `responses` maps agent -> fn(job_id) -> output."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.last_cost = 0.0

    def structured(self, *, agent, system, user, schema, max_tokens=16000):
        assert "# Your skill" in system  # prompt really comes from skills.md
        self.calls.append((agent, user))
        self.last_cost = 0.001
        if agent not in self.responses:
            raise AssertionError(f"unexpected model call for {agent}")
        m = re.search(r'"(?:job_id|project_id)": "([^"]+)"', user)
        return self.responses[agent](m.group(1) if m else None)

    def tool_loop(self, *, agent, system, user, tools, handle, max_steps=40, budget_usd=None):
        """Replays scripted tool calls: responses["<agent>_tools"] = [(name, args), ...]."""
        self.calls.append((agent, user))
        self.last_cost = 0.002
        result = ""
        for step, (name, args) in enumerate(self.responses.get(f"{agent}_tools", [])):
            result, done = handle(name, args)
            if done:
                return result, step + 1
        return result, 0


def qual(job_id, parts=(24, 23, 18, 21), penalty=0, flags=()):
    return QualifierResult(
        job_id=job_id, score=99,
        breakdown=QualifierBreakdown(skill_match=parts[0], ai_deliverable=parts[1], budget_vs_effort=parts[2], client_quality=parts[3], risk_penalty=penalty),
        red_flags=list(flags), decision="pursue", reason="fits",
    )


def est(job_id, price, task_costs=(1.5, 6, 3)):
    return EstimatorResult(
        job_id=job_id,
        tasks=[EstimateTask(name=f"t{i}", est_hours_equiv=1, est_ai_cost_usd=c) for i, c in enumerate(task_costs)],
        total_ai_cost_usd=0, timeline_days=2, bid_price=BidPrice(amount=price),
        margin_pct=0, assumptions=[], confidence="high",
    )


def proposal(job_id, price, words=150, links=(), extra=""):
    body = " ".join(["detail"] * max(words - 14, 0))
    text = f"You need prices from three sites in one Excel sheet. {body} This is an AI-assisted workflow. {extra}".strip()
    return ProposalResult(
        job_id=job_id, proposal_text=text, portfolio_links=list(links),
        bid_price=ProposalPrice(amount=price), timeline_days=2,
        clarifying_questions=["Any logins?"], ai_disclosure_included=True,
    )


@pytest.fixture
def cfg():
    c = load_config()
    c["database"] = ":memory:"
    c["dry_run"] = True                      # tests never depend on live settings
    c["kill_switch"] = False
    c["freelancer"] = {**c["freelancer"], "bid_via_api": False}
    return c


@pytest.fixture
def make_ctx(cfg):
    def _make(responses: dict, **overrides):
        cfg.update(overrides)
        portfolio = [{"title": "E-commerce price tracker", "url": PORTFOLIO_URL, "tags": ["python", "web scraping", "excel"]}]
        return Ctx(cfg=cfg, db=DB(":memory:"), llm=FakeLLM(responses), skills=Skills.load(cfg["skills_file"]), portfolio=portfolio)
    return _make


@pytest.fixture
def sample_jobs():
    return FileSource(ROOT / "data" / "sample_jobs.json").fetch()


@pytest.fixture
def job_by_id(sample_jobs):
    from freelance_agents.schemas import Job
    return {j["job_id"]: Job.model_validate(json.loads(json.dumps(j))) for j in sample_jobs}
