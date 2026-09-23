"""Acceptance tests for task #2992 — EQUIPA never silently downgrades models.

Owner directive 2026-09-22: every Claude call runs on the configured model
(dispatch_config.json). No sonnet/haiku fallback, ever.

  (a) sustained 529/overloaded never rewrites --model and ends in a loud
      OVERLOADED failure (streaming and non-streaming runners)
  (b) reflexion, RLM decompose, OPRO, SIMBA and the ghost scout build their
      claude commands with the configured model
  (c) auto-routing never returns a tier below the configured model
  (d) ForgeSmith produces zero downgrade proposals and refuses to write one
  (e) static guard: no live string literal selects sonnet/haiku

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import equipa.agent_runner as agent_runner
import equipa.config as equipa_config
import equipa.rlm_decompose as rlm_decompose
from equipa.config import (
    DEFAULT_DISPATCH_CONFIG,
    is_downgrade_model,
    resolve_claude_model,
)
from equipa.constants import DEFAULT_MODEL, DEFAULT_ROLE_MODELS
from equipa.reflexion import run_reflexion_agent
from equipa.roles import get_role_model
from equipa.routing import (
    CB_FAILURE_THRESHOLD,
    TIER_ORDER,
    _circuit_breaker_state,
    auto_select_model,
    model_tier_index,
    record_model_outcome,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGURED_MODEL = "claude-opus-5-5[1m]"
# Both shapes must retry on the SAME model. The second also carries a generic
# retryable marker (503) — the shape on which the pre-#2992 code swapped
# --model to sonnet after MAX_529_RETRIES; the first was not retried at all.
OVERLOADED_STDERRS = [
    "API Error: 529 overloaded_error: Overloaded",
    "API Error: 529 overloaded_error (upstream 503 Service Unavailable)",
]


@pytest.fixture
def configured_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make every get_configured_model() call see CONFIGURED_MODEL."""
    config = dict(DEFAULT_DISPATCH_CONFIG, model=CONFIGURED_MODEL)
    monkeypatch.setattr(equipa_config, "load_dispatch_config", lambda _path: config)
    return config


@pytest.fixture(autouse=True)
def _reset_circuit_state():
    _circuit_breaker_state.clear()
    yield
    _circuit_breaker_state.clear()


def _model_of(cmd: list[str]) -> str:
    return cmd[cmd.index("--model") + 1]


def _claude_cmd(model: str) -> list[str]:
    return ["claude", "-p", "do work", "--model", model, "--max-turns", "5"]


# ---------------------------------------------------------------------------
# (a) Sustained 529 never changes --model and fails loudly
# ---------------------------------------------------------------------------


class TestSustained529NeverSwapsModel:
    MAX_RETRIES = 6  # > MAX_529_RETRIES, so the old fallback would have fired

    @pytest.mark.parametrize("stderr", OVERLOADED_STDERRS)
    def test_streaming_retry_keeps_model_and_fails_overloaded(self, monkeypatch, stderr):
        models_seen: list[str] = []

        async def always_overloaded(cmd, **_kwargs):
            models_seen.append(_model_of(cmd))
            return {"success": False, "errors": [stderr],
                    "result_text": "", "num_turns": 0}

        monkeypatch.setattr(agent_runner, "_run_agent_streaming_impl", always_overloaded)
        monkeypatch.setattr(agent_runner, "get_retry_delay", lambda *_a, **_k: 0.0)

        cmd = _claude_cmd(CONFIGURED_MODEL)
        result = asyncio.run(agent_runner.run_agent_streaming_with_retry(
            cmd, max_retries=self.MAX_RETRIES))

        assert models_seen == [CONFIGURED_MODEL] * self.MAX_RETRIES
        assert _model_of(cmd) == CONFIGURED_MODEL
        assert result["success"] is False
        assert result["outcome"] == agent_runner.OVERLOADED_OUTCOME
        assert result["overloaded"] is True
        assert any("OVERLOADED" in err for err in result["errors"])

    @pytest.mark.parametrize("stderr", OVERLOADED_STDERRS)
    def test_non_streaming_run_agent_keeps_model_and_fails_overloaded(
        self, monkeypatch, stderr,
    ):
        models_seen: list[str] = []

        async def fake_exec(*cmd, **_kwargs):
            models_seen.append(_model_of(list(cmd)))

            async def communicate():
                return b"", stderr.encode()

            return SimpleNamespace(returncode=1, communicate=communicate,
                                   kill=lambda: None)

        monkeypatch.setattr(agent_runner.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(agent_runner, "get_retry_delay", lambda *_a, **_k: 0.0)

        cmd = _claude_cmd(CONFIGURED_MODEL)
        result = asyncio.run(agent_runner.run_agent(cmd, max_retries=self.MAX_RETRIES))

        assert models_seen == [CONFIGURED_MODEL] * self.MAX_RETRIES
        assert result["success"] is False
        assert result["outcome"] == agent_runner.OVERLOADED_OUTCOME

    @pytest.mark.parametrize("runner", [
        agent_runner.run_agent,
        agent_runner.run_agent_streaming_with_retry,
    ])
    def test_no_fallback_model_parameter(self, runner):
        assert "fallback_model" not in inspect.signature(runner).parameters


# ---------------------------------------------------------------------------
# (b) Auxiliary Claude calls use the configured model
# ---------------------------------------------------------------------------


class TestAuxiliaryCallsUseConfiguredModel:
    def test_reflexion_uses_configured_model(self, monkeypatch, configured_dispatch):
        captured: list[list[str]] = []

        async def fake_run_agent(cmd, **_kwargs):
            captured.append(list(cmd))
            return {"success": False, "errors": ["stub"]}

        monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
        asyncio.run(run_reflexion_agent(
            {"id": 1, "title": "t"}, {"result_text": "", "num_turns": 1}, "failed"))

        assert len(captured) == 1
        assert _model_of(captured[0]) == CONFIGURED_MODEL

    def test_rlm_sandbox_and_session_use_configured_model(
        self, monkeypatch, configured_dispatch, tmp_path,
    ):
        assert not hasattr(rlm_decompose, "SUB_QUERY_MODELS")
        for role in rlm_decompose.DECOMPOSE_ELIGIBLE_ROLES:
            sandbox = rlm_decompose.ReplSandbox({}, role, str(tmp_path), "")
            assert sandbox._model == CONFIGURED_MODEL

        outer_models: list[str] = []

        def fake_outer(prompt, model, project_dir, timeout=180):
            outer_models.append(model)
            return "Final review, no code blocks."

        monkeypatch.setattr(rlm_decompose, "_call_outer_agent", fake_outer)
        rlm_decompose.run_decompose_session(
            "Review.", str(tmp_path), "integration-tester", repo_files={"a.py": "x = 1"})
        assert outer_models == [CONFIGURED_MODEL]

    def test_rlm_subprocess_commands_carry_given_model(self, monkeypatch, tmp_path):
        commands: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            commands.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout='{"result": "ok"}', stderr="")

        monkeypatch.setattr(rlm_decompose.subprocess, "run", fake_run)
        rlm_decompose._run_sub_query("q", {"a.py": "x"}, CONFIGURED_MODEL, str(tmp_path), "")
        rlm_decompose._call_outer_agent("p", CONFIGURED_MODEL, str(tmp_path))
        assert [_model_of(cmd) for cmd in commands] == [CONFIGURED_MODEL] * 2

    @pytest.mark.parametrize("requested", ["sonnet", "haiku", "claude-sonnet-4-20250514"])
    def test_forgesmith_opro_and_simba_refuse_downgrade_config(
        self, monkeypatch, configured_dispatch, requested,
    ):
        import forgesmith
        import forgesmith_simba

        commands: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            commands.append(list(cmd))
            return SimpleNamespace(returncode=1, stdout="", stderr="stub")

        monkeypatch.setattr(forgesmith.subprocess, "run", fake_run)
        monkeypatch.setattr(forgesmith_simba.subprocess, "run", fake_run)

        forgesmith.call_claude_for_proposals("p", {"opro": {"model": requested}})
        forgesmith_simba.call_claude_for_rules("p", {"simba": {"model": requested}})
        forgesmith.dispatch_ghost_scout("p")

        assert [_model_of(cmd) for cmd in commands] == [CONFIGURED_MODEL] * 3

    def test_mcp_allowlist_is_opus_only(self, configured_dispatch):
        from equipa.mcp_server import _allowed_models

        allowed = _allowed_models()
        assert CONFIGURED_MODEL in allowed
        assert not any(is_downgrade_model(model) for model in allowed)


class TestResolveClaudeModel:
    @pytest.mark.parametrize("requested", [None, "", "sonnet", "haiku",
                                           "claude-3-5-haiku-20241022"])
    def test_downgrade_or_missing_resolves_to_configured(self, configured_dispatch, requested):
        assert resolve_claude_model(requested) == CONFIGURED_MODEL

    def test_only_configured_request_is_honoured(self, configured_dispatch):
        # Task #2994 S3 inverted the denylist to an allowlist: the configured
        # model is honoured, but a bare "opus" alias (which the CLI may map to
        # a different Opus) now resolves to the configured model too.
        assert resolve_claude_model(CONFIGURED_MODEL) == CONFIGURED_MODEL
        assert resolve_claude_model("opus") == CONFIGURED_MODEL

    def test_code_defaults_are_never_downgrades(self):
        assert not is_downgrade_model(DEFAULT_MODEL)
        assert not is_downgrade_model(DEFAULT_DISPATCH_CONFIG["model"])
        assert not [role for role, model in DEFAULT_ROLE_MODELS.items()
                    if is_downgrade_model(model)]


# ---------------------------------------------------------------------------
# (c) Routing never selects below the configured model
# ---------------------------------------------------------------------------

ROUTING_TASKS = [
    {"title": "Fix typo", "description": "simple trivial rename", "priority": "low"},
    {"title": "Docs", "description": "update readme wording", "priority": "medium"},
    {"title": "Refactor", "description": "refactor auth module with tests",
     "priority": "high"},
    {"title": "Architecture", "description": "design distributed consensus migration "
     "across services with security audit", "priority": "critical"},
]


class TestRoutingNeverBelowConfigured:
    @pytest.mark.parametrize("task", ROUTING_TASKS, ids=lambda t: t["title"])
    @pytest.mark.parametrize("configured", [CONFIGURED_MODEL, "opus", "sonnet"])
    def test_auto_select_never_below_configured(self, task, configured):
        chosen = auto_select_model(task, {"model": configured})
        assert chosen is not None
        assert model_tier_index(chosen) >= model_tier_index(configured)

    @pytest.mark.parametrize("task", ROUTING_TASKS, ids=lambda t: t["title"])
    def test_opus_configured_always_routes_to_configured(self, task):
        assert auto_select_model(task, {"model": CONFIGURED_MODEL}) == CONFIGURED_MODEL

    def test_open_circuit_fails_closed_instead_of_falling_down(self):
        for _ in range(CB_FAILURE_THRESHOLD):
            record_model_outcome(CONFIGURED_MODEL, success=False)
        for cheaper in TIER_ORDER[:-1]:
            assert auto_select_model(ROUTING_TASKS[0], {"model": CONFIGURED_MODEL}) != cheaper
        assert auto_select_model(ROUTING_TASKS[0], {"model": CONFIGURED_MODEL}) is None

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_MODELS))
    def test_get_role_model_with_auto_routing_on(self, role):
        config = {"model": CONFIGURED_MODEL, "features": {"auto_model_routing": True}}
        args = SimpleNamespace(model=None)
        assert get_role_model(role, args, config, ROUTING_TASKS[0]) == CONFIGURED_MODEL


# ---------------------------------------------------------------------------
# (d) ForgeSmith produces zero downgrade proposals and refuses downgrade writes
# ---------------------------------------------------------------------------


def _simple_opus_runs(count: int = 50) -> list[dict[str, Any]]:
    return [{"complexity": "simple", "model": "opus", "role": "developer",
             "success": 1} for _ in range(count)]


class TestForgeSmithNeverDowngrades:
    @pytest.fixture
    def forgesmith(self, monkeypatch, tmp_path):
        import forgesmith

        dispatch_path = tmp_path / "dispatch_config.json"
        dispatch_path.write_text(json.dumps({
            "model": CONFIGURED_MODEL, "model_simple": CONFIGURED_MODEL,
            "features": {"auto_model_routing": False},
        }), encoding="utf-8")
        monkeypatch.setattr(forgesmith, "DISPATCH_CONFIG", dispatch_path)
        monkeypatch.setattr(forgesmith, "run_impact_analysis",
                            lambda *_a, **_k: pytest.fail("must refuse before impact analysis"))
        return forgesmith

    def test_analyzer_produces_zero_proposals(self, forgesmith):
        cfg = forgesmith.load_config()
        assert forgesmith.analyze_model_downgrade(_simple_opus_runs(), cfg) == []
        assert "allowed_models" not in cfg["limits"]

    def test_apply_changes_refuses_model_downgrade_finding(self, forgesmith, monkeypatch):
        monkeypatch.setattr(forgesmith, "get_suppressed_changes", lambda _cfg: set())
        monkeypatch.setattr(forgesmith, "apply_config_change",
                            lambda *_a, **_k: pytest.fail("downgrade must never be applied"))
        finding = {"pattern": "model_downgrade", "role": "developer", "success_rate": 1.0,
                   "samples": 50, "current_model": "opus", "proposed_model": "sonnet"}
        cfg = {"max_changes_per_run": 5, "max_prompt_patches_per_run": 2, "limits": {}}
        assert forgesmith.apply_changes([finding], "run-1", cfg) == []

    @pytest.mark.parametrize("key,new_val", [
        ("model_simple", "sonnet"),
        ("model", "claude-haiku-4-5-20251001"),
        ("features", {"auto_model_routing": True}),
    ])
    @pytest.mark.parametrize("dry_run", [True, False])
    def test_apply_config_change_refuses(self, forgesmith, key, new_val, dry_run):
        before = forgesmith.DISPATCH_CONFIG.read_text(encoding="utf-8")
        assert forgesmith.apply_config_change(
            key, "x", new_val, "r", "run-1", {}, dry_run=dry_run) is None
        assert forgesmith.DISPATCH_CONFIG.read_text(encoding="utf-8") == before

    def test_rollback_never_restores_a_downgrade(self, forgesmith, monkeypatch):
        executed: list[str] = []
        fake_conn = SimpleNamespace(execute=lambda sql, *_a: executed.append(sql),
                                    commit=lambda: None, close=lambda: None)
        monkeypatch.setattr(forgesmith, "get_db", lambda write=False: fake_conn)
        before = forgesmith.DISPATCH_CONFIG.read_text(encoding="utf-8")

        forgesmith.rollback_change({
            "id": 7, "change_type": "config_tune",
            "target_file": str(forgesmith.DISPATCH_CONFIG),
            "new_value": CONFIGURED_MODEL, "old_value": "sonnet",
        })

        after = json.loads(forgesmith.DISPATCH_CONFIG.read_text(encoding="utf-8"))
        assert after == json.loads(before)
        assert not any(is_downgrade_model(value) for value in after.values())


# ---------------------------------------------------------------------------
# (e) Static guard: no live code path names sonnet/haiku as a model
# ---------------------------------------------------------------------------

# Files where a sonnet/haiku string literal is data, not a model selection.
_SAFE_LITERAL_FILES = {
    # Claude-leak detector: flags these words in runtime-agnostic manifests.
    "equipa/templates.py",
    # Tier NAMES for complexity scoring; auto_select_model clamps the scored
    # tier up to the configured model, so these never select a model.
    "equipa/routing.py",
    # The downgrade detector itself.
    "equipa/config.py",
    # Standalone-mode fallbacks that REFUSE sonnet/haiku.
    "equipa/mcp_server.py",
    "scripts/forgesmith_simba.py",
    # LITM attention-weight table keyed by historical agent_runs.model values
    # (analytics only; never passed to claude --model).
    "forgesmith_litm.py",
}


def _downgrade_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            value = node.value.strip().lower()
            if value in ("sonnet", "haiku") or value.startswith(("claude-sonnet", "claude-haiku",
                                                                   "claude-3-5-sonnet",
                                                                   "claude-3-5-haiku")):
                hits.append((node.lineno, node.value))
    return hits


def test_no_live_code_selects_sonnet_or_haiku():
    sources = sorted(
        [*REPO_ROOT.joinpath("equipa").rglob("*.py"),
         *REPO_ROOT.glob("forgesmith*.py"),
         *REPO_ROOT.joinpath("scripts").rglob("*.py")]
    )
    offenders = {}
    for path in sources:
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in _SAFE_LITERAL_FILES:
            continue
        hits = _downgrade_literals(path)
        if hits:
            offenders[relative] = hits
    assert offenders == {}, f"sonnet/haiku model literals in live code: {offenders}"
