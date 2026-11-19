"""The single path to every model. LiteLLM -> hosted open-weight APIs only (no self-hosting).

Per call: JSON checked against a pydantic schema, one repair retry, provider failover within a
tier, then escalation to the next tier. Every attempt is logged (tokens, cost, latency, outcome).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

FORBIDDEN_PREFIXES = ("ollama", "ollama_chat", "hosted_vllm", "vllm", "llamafile", "lm_studio", "local")


class SelfHostedModelError(ValueError):
    pass


class GatewayError(RuntimeError):
    pass


def check_hosted_only(model: str) -> None:
    prefix = model.split("/", 1)[0].lower()
    if prefix in FORBIDDEN_PREFIXES:
        raise SelfHostedModelError(f"'{model}' looks self-hosted. Models must be hosted APIs behind LiteLLM.")


@dataclass
class Routing:
    version: str
    tiers: dict[str, list[str]]
    jobs: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: str | Path) -> "Routing":
        raw = yaml.safe_load(Path(path).read_text())
        tiers = {k: list(v["models"]) for k, v in raw["tiers"].items()}
        for models in tiers.values():
            for m in models:
                check_hosted_only(m)
        return cls(str(raw.get("version", "0")), tiers, raw["jobs"])


class RunLog(Protocol):
    def write(self, record: dict[str, Any]) -> None: ...


@dataclass
class JsonlRunLog:
    path: Path

    def write(self, record: dict[str, Any]) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")


@dataclass
class MemoryRunLog:
    records: list[dict[str, Any]] = field(default_factory=list)

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def _extract_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            text = m.group(0)
    return json.loads(text)


class Gateway:
    def __init__(self, routing: Routing, completion: Callable[..., Any] | None = None,
                 run_log: RunLog | None = None):
        if completion is None:
            import litellm
            litellm.suppress_debug_info = True
            litellm.set_verbose = False
            completion = litellm.completion
            self._cost = litellm.completion_cost
        else:
            self._cost = None
        self.routing = routing
        self._completion = completion
        self.run_log = run_log or MemoryRunLog()

    def call_json(self, job: str, messages: list[dict[str, str]], schema: type[T], *, trigger: str = "") -> T:
        cfg = self.routing.jobs[job]
        errors: list[str] = []
        for tier in cfg["tiers"]:
            for model in self.routing.tiers[tier]:
                check_hosted_only(model)
                msgs = list(messages)
                for attempt in (1, 2):  # attempt 2 = the single repair retry
                    t0 = time.monotonic()
                    rec: dict[str, Any] = {"job_type": job, "trigger": trigger, "model": model, "tier": tier,
                                           "attempt": attempt, "config_version": self.routing.version}
                    try:
                        resp = self._completion(model=model, messages=msgs,
                                                max_tokens=cfg.get("max_tokens", 2000),
                                                temperature=cfg.get("temperature", 0.1))
                    except Exception as e:  # provider down / rate limited: fail over to the next model
                        rec.update(error_code="provider_error", error=str(e)[:300],
                                   latency_ms=int((time.monotonic() - t0) * 1000))
                        self.run_log.write(rec)
                        errors.append(f"{model}: {e}")
                        break
                    content = resp.choices[0].message.content or ""
                    usage = getattr(resp, "usage", None)
                    rec.update(tokens_in=getattr(usage, "prompt_tokens", None),
                               tokens_out=getattr(usage, "completion_tokens", None),
                               latency_ms=int((time.monotonic() - t0) * 1000))
                    if self._cost:
                        try:
                            rec["cost_usd"] = self._cost(completion_response=resp)
                        except Exception:
                            rec["cost_usd"] = None
                    try:
                        parsed = schema.model_validate(_extract_json(content))
                        rec["ok"] = True
                        self.run_log.write(rec)
                        return parsed
                    except (json.JSONDecodeError, ValidationError) as e:
                        rec.update(ok=False, error_code="invalid_output", error=str(e)[:300])
                        self.run_log.write(rec)
                        errors.append(f"{model} attempt {attempt}: invalid output")
                        msgs = [*messages, {"role": "assistant", "content": content},
                                {"role": "user", "content": f"Your reply was invalid: {str(e)[:500]}\n"
                                 "Reply again with ONLY the corrected JSON object."}]
        raise GatewayError(f"All models failed for job '{job}': " + " | ".join(errors[-6:]))
