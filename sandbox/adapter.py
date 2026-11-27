"""The TargetAppAdapter protocol every demo domain implements, plus the shared SQLite plumbing.

    reset_state(case_id, seed) -> db_path      fresh copy of the case's seeded database
    run_agent(case_id, flags, identity, db_path, seed) -> Trace
    grade(trace, db_path) -> GradeResult        deterministic: database + tool log, no LLM

Replays pass flags in directly (captured config plus overrides). Live Flagsmith is never read
here; see sandbox/live.py for the SDK path the running agents use.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from contracts.models import Domain, FlagConfig, GradeResult, Identity, Trace
from sandbox import llm
from sandbox.flags import HARMLESS, effective_values


class TargetAppAdapter(Protocol):
    domain: Domain

    def case_ids(self, kind: str | None = None) -> list[str]: ...
    def identity(self, case_id: str) -> Identity: ...
    def reset_state(self, case_id: str, seed: int = 0) -> Path: ...
    def run_agent(self, case_id: str, flags: FlagConfig, identity: Identity, db_path: Path,
                  seed: int = 0) -> Trace: ...
    def grade(self, trace: Trace, db_path: Path) -> GradeResult: ...


@dataclass
class AgentSettings:
    """What the flags decide for one run, after segment overrides and the kill switch."""

    model_flag: str
    prompt_version: str
    tool_policy: str
    max_steps: int
    context_mode: str
    temperature: float
    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_flags(cls, flags: FlagConfig, identity: Identity) -> AgentSettings:
        v = effective_values(flags, identity)
        resolved = FlagConfig(values=v, source=flags.source)
        return cls(model_flag=resolved.get_str("agent_model"),
                   prompt_version=resolved.get_str("agent_prompt_version"),
                   tool_policy=resolved.get_str("agent_tool_policy"),
                   max_steps=resolved.get_int("agent_max_steps"),
                   context_mode=resolved.get_str("agent_context_mode"),
                   temperature=resolved.get_float("agent_temperature"),
                   values=v)


class SqliteDomain:
    """Shared plumbing: cases from JSON, a seeded template database per case, fresh copies per
    run, and the tool loop. Subclasses supply schema, seed, tools, prompts and the grader."""

    domain: Domain
    root: Path  # the domain package directory (schema.sql, cases/, prompts/)

    def __init__(self, provider: llm.Provider | None = None, *,
                 workdir: Path | None = None, env: Mapping[str, str] | None = None,
                 timeout_s: float = 120.0):
        self._provider = provider
        self.env = env
        self.timeout_s = timeout_s
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix=f"trustgate-{self.domain}-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._templates: dict[tuple[str, int], Path] = {}
        self._lock = threading.Lock()

    # -- provider (lazy: unit tests that never run the agent need no key) --------------------
    @property
    def provider(self) -> llm.Provider:
        if self._provider is None:
            self._provider = llm.provider_from_env(self.env)
        return self._provider

    # -- cases --------------------------------------------------------------------------------
    def case_ids(self, kind: str | None = None) -> list[str]:
        ids = []
        for path in sorted((self.root / "cases").glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            if kind is None or case.get("kind") == kind:
                ids.append(case["id"])
        return ids

    def load_case(self, case_id: str) -> dict[str, Any]:
        path = self.root / "cases" / f"{case_id}.json"
        if not path.exists():
            raise KeyError(f"no {self.domain} case {case_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    # -- state --------------------------------------------------------------------------------
    def seed(self, conn: sqlite3.Connection, case: dict[str, Any], seed: int) -> None:
        raise NotImplementedError

    def reset_state(self, case_id: str, seed: int = 0) -> Path:
        key = (case_id, seed)
        with self._lock:
            template = self._templates.get(key)
            if template is None:
                template = self.workdir / f"template-{case_id}-{seed}.db"
                template.unlink(missing_ok=True)
                conn = sqlite3.connect(template)
                try:
                    conn.executescript((self.root / "schema.sql").read_text(encoding="utf-8"))
                    self.seed(conn, self.load_case(case_id), seed)
                    conn.commit()
                finally:
                    conn.close()
                self._templates[key] = template
        run_db = self.workdir / f"run-{case_id}-{uuid.uuid4().hex[:12]}.db"
        shutil.copyfile(template, run_db)
        return run_db

    # -- the agent ------------------------------------------------------------------------------
    def system_prompt(self, settings: AgentSettings) -> str:
        path = self.root / "prompts" / f"{settings.prompt_version}.md"
        if not path.exists():
            raise KeyError(f"no prompt {settings.prompt_version!r} for {self.domain}")
        prompt = path.read_text(encoding="utf-8")
        shown = {k: v for k, v in settings.values.items() if k in HARMLESS}
        if shown:  # scaling-test flags: read by the agent, formatting only
            settings_line = ", ".join(f"{k}={v}" for k, v in sorted(shown.items()))
            prompt += ("\nFormatting settings (cosmetic, they don't change your rules): "
                       f"{settings_line}\n")
        return prompt

    def user_message(self, case: dict[str, Any], settings: AgentSettings) -> str:
        raise NotImplementedError

    def tools(self, settings: AgentSettings) -> tuple[list[llm.ToolSpec],
                                                     Callable[[sqlite3.Connection, str,
                                                               dict[str, Any]], Any]]:
        raise NotImplementedError

    def reported_status(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute("SELECT status FROM status_reports ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def run_agent(self, case_id: str, flags: FlagConfig, identity: Identity, db_path: Path,
                  seed: int = 0) -> Trace:
        case = self.load_case(case_id)
        settings = AgentSettings.from_flags(flags, identity)
        real_model = llm.model_id(settings.model_flag, self.env)
        specs, handler = self.tools(settings)
        allowed = {s.name for s in specs}
        started = time.monotonic()
        conn = sqlite3.connect(db_path)
        try:
            def dispatch(name: str, args: dict[str, Any]) -> Any:
                if name not in allowed:  # the model may hallucinate a tool it wasn't given
                    return {"ok": False, "error": f"unknown tool {name!r}"}
                out = handler(conn, name, args)
                conn.commit()
                return out

            error: str | None = None
            try:
                chat = self.provider.open_chat(model=real_model,
                                               system=self.system_prompt(settings),
                                               user=self.user_message(case, settings),
                                               tools=specs, temperature=settings.temperature,
                                               seed=seed)
                result = llm.run_tool_loop(chat, dispatch, max_steps=settings.max_steps,
                                           timeout_s=self.timeout_s)
                error = result.error
            except llm.LLMError as exc:
                result = llm.LoopResult([], "", 0, 0, 0, False, str(exc))
                error = str(exc)
            status = self.reported_status(conn)
        finally:
            conn.close()
        return Trace(run_id=uuid.uuid4().hex, domain=self.domain, case_id=case_id, flags=flags,
                     identity=identity, tool_calls=result.tool_calls, reported_status=status,
                     model=real_model, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                     cost_usd=llm.cost_usd(settings.model_flag, result.tokens_in,
                                           result.tokens_out, self.env),
                     duration_s=round(time.monotonic() - started, 3), error=error)
