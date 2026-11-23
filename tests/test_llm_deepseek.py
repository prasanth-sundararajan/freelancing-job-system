from types import SimpleNamespace

import pytest

from freelance_agents.llm import LLM
from freelance_agents.schemas import SubmitResult

GOOD = '{"job_id": "j", "channel": "api", "status": "submitted"}'
BAD = '{"job_id": "j", "channel": "fax"}'


class FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=100, prompt_cache_hit_tokens=800)
        msg = SimpleNamespace(content=self.contents.pop(0))
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def make(cfg, contents):
    completions = FakeCompletions(contents)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    cfg["provider"] = "deepseek"
    return LLM(cfg, client=client), completions


def test_deepseek_valid_json(cfg):
    llm, comp = make(cfg, [GOOD])
    out = llm.structured(agent="submitter", system="sys", user="u", schema=SubmitResult)
    assert out.status == "submitted"
    assert comp.calls[0]["response_format"] == {"type": "json_object"}
    assert llm.last_cost > 0


def test_deepseek_repairs_invalid_json_once(cfg):
    llm, comp = make(cfg, [BAD, GOOD])
    assert llm.structured(agent="submitter", system="sys", user="u", schema=SubmitResult).channel == "api"
    assert "failed validation" in comp.calls[1]["messages"][-1]["content"]


def test_deepseek_tool_loop(cfg):
    def call(name, args, cid):
        return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))

    turns = [
        [call("write_file", '{"path": "a.py", "content": "x=1"}', "c1")],
        [call("finish", '{"status": "done", "notes": "ok"}', "c2")],
    ]

    class Completions:
        def __init__(self):
            self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            msg = SimpleNamespace(content="", tool_calls=turns.pop(0), reasoning_content=None)
            usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10, prompt_cache_hit_tokens=0)
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])

    comp = Completions()
    cfg["provider"] = "deepseek"
    llm = LLM(cfg, client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    seen = []

    def handle(name, args):
        seen.append((name, args))
        return ("finished", True) if name == "finish" else ("ok", False)

    result, steps = llm.tool_loop(agent="builder", system="s", user="u", tools=[], handle=handle)
    assert result == "finished" and steps == 2
    assert seen == [("write_file", {"path": "a.py", "content": "x=1"}), ("finish", {"status": "done", "notes": "ok"})]
    assert {"role": "tool", "tool_call_id": "c1", "content": "ok"} in comp.calls[1]["messages"]
    assert comp.calls[1]["messages"][2]["tool_calls"][0]["id"] == "c1"
    with pytest.raises(Exception):
        LLM(cfg, client=SimpleNamespace(chat=SimpleNamespace(completions=comp))).tool_loop(
            agent="builder", system="s", user="u", tools=[], handle=handle, budget_usd=0)


def test_deepseek_gives_up_after_repair(cfg):
    llm, _ = make(cfg, [BAD, BAD])
    with pytest.raises(ValueError):
        llm.structured(agent="submitter", system="sys", user="u", schema=SubmitResult)
