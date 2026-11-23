import json
import zipfile

import pytest

from conftest import est
from freelance_agents import guardrails as g
from freelance_agents.agents import ScoutAgent
from freelance_agents.delivery import DeliveryPipeline
from freelance_agents.schemas import (
    AcceptanceCriterion,
    CriterionResult,
    DeliveryResult,
    PlanResult,
    QAIssue,
    QAResult,
    QAScores,
    TaskNode,
)

JID = "freelancer:88231"


class FakeSandbox:
    def __init__(self, root, test_exit_codes):
        self.root, self.files_, self.codes, self.commands = root, {}, list(test_exit_codes), []

    def write(self, rel, content):
        self.files_[rel] = content

    def read(self, rel):
        return self.files_[rel]

    def files(self):
        return sorted(self.files_)

    def all_text(self):
        return "\n".join(self.files_.values())

    def run(self, command):
        self.commands.append(command)
        if command.startswith("pytest"):
            code = self.codes.pop(0) if self.codes else 0
            return code, "1 passed" if code == 0 else "1 failed"
        return 0, ""

    def package(self, dest, extra=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as z:
            for rel, text in {**self.files_, **(extra or {})}.items():
                z.writestr(rel, text)
        return dest


def plan(jid, questions=()):
    return PlanResult(project_id=jid, spec_md="Scrape 3 sites to Excel",
                      acceptance_criteria=[AcceptanceCriterion(id="AC1", criterion="tests pass", test_method="automated")],
                      task_graph=[TaskNode(id="T1", description="scraper", depends_on=[], builder="code")],
                      tech_stack=["python"], open_questions=list(questions))


def qa(jid, ok=True):
    return QAResult(project_id=jid, verdict="pass" if ok else "fail",
                    criteria_results=[CriterionResult(id="AC1", result="pass" if ok else "fail", evidence="pytest")],
                    issues=[] if ok else [QAIssue(severity="major", description="price parsing", fix="cast to float")],
                    scores=QAScores(functionality=90, quality=90, security=90, originality=90), round=1)


def delivery(jid):
    return DeliveryResult(project_id=jid, action="deliver", handoff_notes_md="## Setup\n## Run\n## Known limitations",
                          revisions_used=0)


BUILD = [("write_file", {"path": "scraper.py", "content": "def parse(p): return float(p)"}),
         ("write_file", {"path": "test_scraper.py", "content": "def test(): assert True"}),
         ("run", {"command": "pytest -q"}),
         ("finish", {"status": "done", "notes": "ok", "tests_added": ["test"]})]


def won_job(make_ctx, sample_jobs, responses, tmp_path, codes=(0,)):
    ctx = make_ctx(responses)
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    ctx.db.set_state(JID, "submitted")
    ctx.db.save_output(JID, "estimator", est(JID, 150).model_dump())
    sandbox = FakeSandbox(tmp_path / "proj", codes)
    pipe = DeliveryPipeline(ctx, sandbox_factory=lambda jid: sandbox)
    pipe.start(JID, notes="3 sites, daily rerun")
    return ctx, pipe, sandbox


def test_happy_path_delivers_package(make_ctx, sample_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr("freelance_agents.delivery.ROOT", tmp_path)
    responses = {"planner": plan, "qa": qa, "delivery": delivery, "builder_tools": BUILD}
    ctx, pipe, sandbox = won_job(make_ctx, sample_jobs, responses, tmp_path)
    assert ctx.db.get_job(JID)["state"] == "planning"
    result = pipe.run(JID)
    assert result.startswith("delivered")
    assert ctx.db.get_job(JID)["state"] == "delivered"
    assert "pytest -q" in sandbox.commands  # QA ran the tests itself
    pkg = json.loads(ctx.db.pending_approvals("delivery")[0]["payload"])
    names = zipfile.ZipFile(pkg["zip"]).namelist()
    assert {"scraper.py", "test_scraper.py", "HANDOFF.md"} <= set(names)


def test_failing_tests_loop_then_escalate(make_ctx, sample_jobs, tmp_path, monkeypatch):
    monkeypatch.setattr("freelance_agents.delivery.ROOT", tmp_path)
    responses = {"planner": plan, "qa": lambda jid: qa(jid, ok=True), "delivery": delivery, "builder_tools": BUILD}
    ctx, pipe, _ = won_job(make_ctx, sample_jobs, responses, tmp_path, codes=(1,) * 10)  # builder + QA runs all fail
    # QA model says "pass" but the tests fail: the code guardrail forces "fail" every round.
    assert pipe.run(JID).startswith("escalated")
    assert ctx.db.get_job(JID)["state"] == "escalated"


def test_open_questions_pause_in_planning(make_ctx, sample_jobs, tmp_path):
    responses = {"planner": lambda jid: plan(jid, questions=["Which 3 sites?"])}
    ctx, pipe, _ = won_job(make_ctx, sample_jobs, responses, tmp_path)
    assert "waiting" in pipe.run(JID)
    assert ctx.db.get_job(JID)["state"] == "planning"
    assert ctx.db.pending_approvals("questions")


def test_builder_secret_blocks(make_ctx, sample_jobs, tmp_path):
    leaky = [("write_file", {"path": "cfg.py", "content": "api_key = 'sk-ant-abcdefghijklmnop'"}),
             ("finish", {"status": "done", "notes": "ok"})]
    ctx, pipe, _ = won_job(make_ctx, sample_jobs, {"planner": plan, "builder_tools": leaky}, tmp_path)
    assert pipe.run(JID).startswith("escalated")


def test_cannot_start_unsubmitted_job(make_ctx, sample_jobs, tmp_path):
    ctx = make_ctx({})
    ScoutAgent(ctx).ingest("file", sample_jobs[:1])
    with pytest.raises(ValueError):
        DeliveryPipeline(ctx).start(JID)
    assert not g.can_transition("discovered", "won")
