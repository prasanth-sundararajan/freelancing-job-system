"""Model access for all agents: structured output, caching, cost tracking.

Two providers, picked by `provider` in config.yaml:
- anthropic: Claude via the Anthropic SDK (schema-constrained structured output)
- deepseek:  DeepSeek via its OpenAI-compatible API (JSON mode + Pydantic validation)
"""
from __future__ import annotations

import json
import os
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Claude models that support the server-side refusal fallback.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


class AgentRefusal(Exception):
    """The model declined the request."""


class MissingAPIKey(Exception):
    pass


class BudgetExceeded(Exception):
    """A tool loop spent its whole cost budget."""


class LLM:
    def __init__(self, cfg: dict, client=None):
        self.cfg = cfg
        self.provider = cfg.get("provider", "anthropic")
        self._client = client
        self.last_cost = 0.0

    def check_key(self) -> None:
        name = KEY_ENV[self.provider]
        if self._client is None and not os.getenv(name):
            raise MissingAPIKey(f"{name} is not set. Run: .\\.venv\\Scripts\\python.exe scripts\\set_key.py {name}")

    @property
    def client(self):
        if self._client is None:
            self.check_key()
            if self.provider == "deepseek":
                import openai

                self._client = openai.OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url=self.cfg["deepseek"]["base_url"])
            else:
                import anthropic

                self._client = anthropic.Anthropic()
        return self._client

    def structured(self, *, agent: str, system: str, user: str, schema: type[T], max_tokens: int = 16000) -> T:
        self.last_cost = 0.0
        if self.provider == "deepseek":
            return self._deepseek(agent, system, user, schema, max_tokens)
        return self._anthropic(agent, system, user, schema, max_tokens)

    # --- Anthropic -----------------------------------------------------------
    def _anthropic(self, agent: str, system: str, user: str, schema: type[T], max_tokens: int) -> T:
        model = self.cfg["models"][agent]
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        effort = self.cfg.get("effort", {}).get(agent)
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if model in _FALLBACK_MODELS:
            kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
            kwargs["extra_body"] = {"fallbacks": "default"}

        response = self.client.messages.parse(**kwargs)
        u = response.usage
        p = self.cfg["pricing_per_mtok"][model]
        self.last_cost = round(
            (u.input_tokens * p["input"] + u.output_tokens * p["output"]
             + (u.cache_read_input_tokens or 0) * p["cache_read"]
             + (u.cache_creation_input_tokens or 0) * p["input"] * 1.25) / 1_000_000,
            6,
        )
        if response.stop_reason == "refusal":
            raise AgentRefusal(f"{agent}: model declined the request")
        if response.stop_reason == "max_tokens":
            raise ValueError(f"{agent}: output hit max_tokens before finishing")
        if response.parsed_output is None:
            raise ValueError(f"{agent}: no structured output returned")
        return response.parsed_output

    # --- Tool loop (Builder) -----------------------------------------------------
    def tool_loop(self, *, agent: str, system: str, user: str, tools: list[dict], handle,
                  max_steps: int = 40, budget_usd: float | None = None) -> tuple[str, int]:
        """Let the model call tools until `handle` reports done or it stops calling tools.

        tools: [{"name", "description", "parameters": JSON schema}]
        handle(name, args) -> (result_text, done)
        Returns (final text or the done-tool result, steps used). Cost accumulates in last_cost.
        """
        self.last_cost = 0.0
        run = self._ds_tools if self.provider == "deepseek" else self._anthropic_tools
        return run(agent, system, user, tools, handle, max_steps, budget_usd)

    def _check_budget(self, budget_usd: float | None) -> None:
        if budget_usd is not None and self.last_cost >= budget_usd:
            raise BudgetExceeded(f"spent ${self.last_cost:.4f} of ${budget_usd:.4f} budget")

    def _ds_tools(self, agent, system, user, tools, handle, max_steps, budget_usd):
        ds = self.cfg["deepseek"]
        model = ds["models"].get(agent, ds["default_model"])
        price = ds["pricing_per_mtok"][model]
        oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["parameters"]}} for t in tools]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for step in range(max_steps):
            self._check_budget(budget_usd)
            resp = self.client.chat.completions.create(model=model, messages=messages, tools=oa_tools,
                                                       max_tokens=ds["max_output_tokens"])
            u = resp.usage
            hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
            self.last_cost = round(self.last_cost + ((u.prompt_tokens - hit) * price["input"] + hit * price["cache_hit"]
                                                     + u.completion_tokens * price["output"]) / 1_000_000, 6)
            choice = resp.choices[0]
            if choice.finish_reason == "content_filter":
                raise AgentRefusal(f"{agent}: blocked by content filter")
            msg = choice.message
            calls = msg.tool_calls or []
            assistant: dict = {"role": "assistant", "content": msg.content or ""}
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                assistant["reasoning_content"] = reasoning
            if calls:
                assistant["tool_calls"] = [{"id": c.id, "type": "function",
                                            "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                           for c in calls]
            messages.append(assistant)
            if not calls:
                return msg.content or "", step + 1
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                    result, done = handle(c.function.name, args)
                except json.JSONDecodeError:
                    result, done = "error: tool arguments were not valid JSON", False
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result[:8000]})
                if done:
                    return result, step + 1
        raise ValueError(f"{agent}: stopped after {max_steps} steps without finishing")

    def _anthropic_tools(self, agent, system, user, tools, handle, max_steps, budget_usd):
        model = self.cfg["models"][agent]
        p = self.cfg["pricing_per_mtok"][model]
        a_tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        messages: list[dict] = [{"role": "user", "content": user}]
        extra: dict = {}
        effort = self.cfg.get("effort", {}).get(agent)
        if effort:
            extra["output_config"] = {"effort": effort}
        for step in range(max_steps):
            self._check_budget(budget_usd)
            with self.client.messages.stream(
                model=model, max_tokens=64000, tools=a_tools, messages=messages,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}], **extra,
            ) as stream:
                resp = stream.get_final_message()
            u = resp.usage
            self.last_cost = round(self.last_cost + (
                u.input_tokens * p["input"] + u.output_tokens * p["output"]
                + (u.cache_read_input_tokens or 0) * p["cache_read"]
                + (u.cache_creation_input_tokens or 0) * p["input"] * 1.25) / 1_000_000, 6)
            if resp.stop_reason == "refusal":
                raise AgentRefusal(f"{agent}: model declined the request")
            messages.append({"role": "assistant", "content": resp.content})  # keeps thinking blocks intact
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                return "".join(b.text for b in resp.content if b.type == "text"), step + 1
            results, finished = [], None
            for b in uses:
                result, done = handle(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result[:8000]})
                if done:
                    finished = result
            messages.append({"role": "user", "content": results})
            if finished is not None:
                return finished, step + 1
        raise ValueError(f"{agent}: stopped after {max_steps} steps without finishing")

    # --- DeepSeek ------------------------------------------------------------
    def _deepseek(self, agent: str, system: str, user: str, schema: type[T], max_tokens: int) -> T:
        ds = self.cfg["deepseek"]
        model = ds["models"].get(agent, ds["default_model"])
        # The schema goes in the system prompt so the prefix stays identical per
        # agent; DeepSeek caches repeated prefixes automatically.
        schema_text = json.dumps(schema.model_json_schema(), sort_keys=True)
        messages = [
            {"role": "system", "content": f"{system}\n\nReturn one json object that validates against this JSON Schema:\n{schema_text}"},
            {"role": "user", "content": user},
        ]
        price = ds["pricing_per_mtok"][model]
        last_error = ""
        for _ in range(2):  # one repair attempt if the JSON doesn't validate
            resp = self.client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=min(max_tokens, ds["max_output_tokens"]),
            )
            u = resp.usage
            hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
            self.last_cost = round(
                self.last_cost
                + ((u.prompt_tokens - hit) * price["input"] + hit * price["cache_hit"] + u.completion_tokens * price["output"]) / 1_000_000,
                6,
            )
            choice = resp.choices[0]
            content = choice.message.content or ""
            if choice.finish_reason == "content_filter":
                raise AgentRefusal(f"{agent}: blocked by content filter")
            try:
                return schema.model_validate_json(content)
            except ValidationError as e:
                last_error = str(e)[:1500]
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That JSON failed validation:\n{last_error}\nReturn the corrected json object only."},
                ]
        raise ValueError(f"{agent}: invalid JSON after repair attempt: {last_error[:300]}")
