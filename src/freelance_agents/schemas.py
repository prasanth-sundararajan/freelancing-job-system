"""Pydantic models for every agent's Response section in skills.md."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# --- Scout -------------------------------------------------------------------
class Budget(BaseModel):
    type: Literal["fixed", "hourly"]
    min: float = 0
    max: float = 0
    currency: str = "USD"
    usd_rate: float = 1.0  # USD value of 1 unit of `currency` (Freelancer's exchange_rate)


class Competition(BaseModel):
    bid_count: Optional[int] = None
    bid_avg: Optional[float] = None  # in the project's currency


class ClientInfo(BaseModel):
    country: Optional[str] = None
    rating: Optional[float] = None
    hires: Optional[int] = None
    spent: Optional[float] = None
    payment_verified: Optional[bool] = None


class Job(BaseModel):
    job_id: str
    platform: Literal["freelancer", "upwork", "fiverr", "other"]
    url: str
    title: str
    description: str
    skills: list[str] = []
    budget: Budget
    deadline: Optional[str] = None
    client: ClientInfo = ClientInfo()
    competition: Competition = Competition()
    posted_at: str
    source: Literal["api", "rss", "email", "file"]


# --- Orchestrator ------------------------------------------------------------
class OrchestratorDecision(BaseModel):
    job_id: str
    from_state: str
    to_state: str
    next_agent: Optional[str] = None
    reason: str
    escalate: bool = False


# --- Qualifier ---------------------------------------------------------------
class QualifierBreakdown(BaseModel):
    skill_match: int
    ai_deliverable: int
    budget_vs_effort: int
    client_quality: int
    risk_penalty: int


class QualifierResult(BaseModel):
    job_id: str
    score: int
    breakdown: QualifierBreakdown
    red_flags: list[str]
    decision: Literal["pursue", "skip"]
    reason: str


# --- Estimator ---------------------------------------------------------------
class EstimateTask(BaseModel):
    name: str
    est_hours_equiv: float
    est_ai_cost_usd: float


class BidPrice(BaseModel):
    amount: float
    currency: str = "USD"
    type: Literal["fixed", "hourly"] = "fixed"


class EstimatorResult(BaseModel):
    job_id: str
    tasks: list[EstimateTask]
    total_ai_cost_usd: float
    timeline_days: int
    bid_price: BidPrice
    margin_pct: float
    assumptions: list[str]
    confidence: Literal["low", "medium", "high"]


# --- Proposal Writer ---------------------------------------------------------
class ProposalPrice(BaseModel):
    amount: float
    currency: str = "USD"


class ProposalResult(BaseModel):
    job_id: str
    proposal_text: str
    portfolio_links: list[str]
    bid_price: ProposalPrice
    timeline_days: int
    clarifying_questions: list[str]
    ai_disclosure_included: bool


# --- Submitter ---------------------------------------------------------------
class SubmitResult(BaseModel):
    job_id: str
    channel: Literal["api", "human_approval_queue"]
    status: Literal["submitted", "queued", "failed"]
    platform_bid_id: Optional[str] = None
    credits_spent: float = 0
    error: Optional[str] = None


# --- Client Communication ----------------------------------------------------
class PriceChange(BaseModel):
    amount: float = 0
    approved: bool = False


class ClientReply(BaseModel):
    job_id: str
    reply_text: str
    intent_detected: Literal["question", "negotiation", "scope_change", "acceptance", "complaint", "other"]
    requirements_update: list[str]
    price_change: PriceChange
    escalate: bool = False
    escalation_reason: Optional[str] = None


# --- Planner -----------------------------------------------------------------
class AcceptanceCriterion(BaseModel):
    id: str
    criterion: str
    test_method: Literal["automated", "rubric", "manual"]


class TaskNode(BaseModel):
    id: str
    description: str
    depends_on: list[str]
    builder: Literal["code", "content", "design", "data"]


class PlanResult(BaseModel):
    project_id: str
    spec_md: str
    acceptance_criteria: list[AcceptanceCriterion]
    task_graph: list[TaskNode]
    tech_stack: list[str]
    open_questions: list[str]


# --- Builder -----------------------------------------------------------------
class Artifact(BaseModel):
    path: str
    type: Literal["code", "doc", "image", "data"]


class BuilderResult(BaseModel):
    project_id: str
    task_ids: list[str]
    status: Literal["done", "blocked"]
    artifacts: list[Artifact]
    tests_added: list[str]
    notes: str
    blocked_reason: Optional[str] = None
    cost_used_usd: float = 0


# --- QA ----------------------------------------------------------------------
class CriterionResult(BaseModel):
    id: str
    result: Literal["pass", "fail"]
    evidence: str


class QAIssue(BaseModel):
    severity: Literal["critical", "major", "minor"]
    description: str
    fix: str


class QAScores(BaseModel):
    functionality: int
    quality: int
    security: int
    originality: int


class QAResult(BaseModel):
    project_id: str
    verdict: Literal["pass", "fail"]
    criteria_results: list[CriterionResult]
    issues: list[QAIssue]
    scores: QAScores
    round: int


# --- Delivery ----------------------------------------------------------------
class RevisionRequest(BaseModel):
    items: list[str]
    in_scope: bool


class DeliveryResult(BaseModel):
    project_id: str
    action: Literal["deliver", "revise", "close"]
    package_url: Optional[str] = None
    handoff_notes_md: str
    revision_request: Optional[RevisionRequest] = None
    revisions_used: int


# --- Ops (watchdog) ----------------------------------------------------------
class OpsDiagnosis(BaseModel):
    summary: str
    likely_cause: str
    safe_auto_fix: Literal["none", "restart_bot", "restart_chrome", "recreate_task", "rerun_cycle"]
    user_action: Optional[str] = None


# --- Finance -----------------------------------------------------------------
class UnpaidMilestone(BaseModel):
    project_id: str
    amount: float
    days_overdue: int


class ConfigSuggestion(BaseModel):
    key: str
    current: float
    suggested: float
    reason: str


class FinanceReport(BaseModel):
    period: str
    revenue_usd: float
    ai_cost_usd: float
    platform_fees_usd: float
    infra_cost_usd: float
    profit_usd: float
    win_rate_pct: float
    avg_rating: float
    unpaid_milestones: list[UnpaidMilestone]
    insights: list[str]
    config_suggestions: list[ConfigSuggestion]
