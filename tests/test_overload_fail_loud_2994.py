#!/usr/bin/env python3
"""Task #2994 — fix-forward of #2992 (SECURITY-REVIEW-2992 S1-S9).

Acceptance proofs:

* S1  — a sustained 529 on ANY dispatched agent fails loudly as
        ``agent_overloaded``; it is never parsed into ``no_tests``, a clean
        security review, a fix plan, a goal evaluation or a reflection.
        Covers: tester (dev-test loop), security reviewer (parallel and
        single-task gates), preflight debugger/planner, manager planner and
        evaluator, reflexion, RLM decomposition, and the outcome→status maps.
* S2  — model resolution reads the orchestrator's config, never the CWD.
* S3  — allowlist: only the configured model or an approved upgrade runs.
* S4/S5 — autoresearch tiers 2/3 use the configured model and never put the
        API key on a command line.
* S7  — project-role frontmatter ``model`` goes through the allowlist.
* S9  — persistent-retry mode is bounded and fails loudly.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import equipa.agent_runner as agent_runner
import equipa.cli as cli
import equipa.config as equipa_config
import equipa.loops as loops
import equipa.manager as manager
import equipa.preflight as preflight
import equipa.reflexion as reflexion
import equipa.rlm_decompose as rlm_decompose
import equipa.roles as roles
from equipa.agent_runner import OVERLOADED_OUTCOME, is_overloaded_result
from equipa.config import (
    APPROVED_MODEL_UPGRADES_KEY,
    DEFAULT_DISPATCH_CONFIG,
    DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS,
    DISPATCH_CONFIG_ENV_VAR,
    PERSISTENT_RETRY_MAX_ATTEMPTS_KEY,
    get_configured_model,
    get_persistent_retry_max_attempts,
    is_model_allowed,
    resolve_claude_model,
    set_active_dispatch_config,
)
from equipa.dispatch import run_parallel_tasks

# Shared parallel-mode harness (tests/ is on sys.path via pytest rootdir).
from test_dispatch_parallel_security_review import (
    _make_args,
    _patch_parallel_mode,
    _write_review,
)

CONFIGURED_MODEL = "claude-opus-5-5[1m]"
APPROVED_UPGRADE = "claude-fable-5-1"
OVERLOADED_STDERR = "API Error: 529 overloaded_error: Overloaded"


def _overloaded_result() -> dict[str, Any]:
    """The exact shape run_agent / run_agent_streaming_with_retry return."""
    return agent_runner._fail_overloaded(
        {"success": False, "result_text": "", "num_turns": 0, "cost": 0.0,
         "duration": 0.0, "errors": [OVERLOADED_STDERR]},
        ["claude", "--model", CONFIGURED_MODEL], 10, 10,
    )


@pytest.fixture(autouse=True)
def _isolated_model_config(tmp_path, monkeypatch):
    """Pin model resolution to a temp dispatch config; never a real file."""
    config_path = tmp_path / "orchestrator_dispatch_config.json"
    config_path.write_text(json.dumps({"model": CONFIGURED_MODEL}))
    monkeypatch.setenv(DISPATCH_CONFIG_ENV_VAR, str(config_path))
    set_active_dispatch_config(None)
    yield config_path
    set_active_dispatch_config(None)


# ===========================================================================
# S1 — tester sustained 529 in the dev-test loop
# ===========================================================================


class _FakeConn:
    def execute(self, *_args, **_kwargs):
        return self

    def commit(self):
        pass

    def close(self):
        pass


def _stub_dev_test_loop(monkeypatch, dispatch_results: list[dict]) -> list[dict]:
    """Stub run_dev_test_loop's dependencies; dispatch returns queued results."""
    calls: list[dict] = []

    async def _async_none(*_a, **_kw):
        return None

    async def _async_empty(*_a, **_kw):
        return ""

    async def _preflight_ok(*_a, **_kw):
        return (True, "python", None)

    async def _head_sha(*_a, **_kw):
        return "HEAD"

    async def _fake_dispatch(*_a, **kwargs):
        calls.append(kwargs)
        return dispatch_results[len(calls) - 1]

    @contextlib.contextmanager
    def _fake_cli(*_a, **_kw):
        yield ["claude", "--model", CONFIGURED_MODEL]

    monkeypatch.setattr(loops, "auto_install_dependencies", _async_none)
    monkeypatch.setattr(loops, "preflight_build_check", _preflight_ok)
    monkeypatch.setattr(loops, "get_db_connection", lambda *a, **kw: _FakeConn())
    monkeypatch.setattr(loops, "get_task_complexity", lambda _t: "simple")
    monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: CONFIGURED_MODEL)
    monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 10)
    monkeypatch.setattr(loops, "calculate_dynamic_budget",
                        lambda max_turns, effort=None: (max_turns, max_turns))
    monkeypatch.setattr(loops, "load_checkpoint", lambda *a, **kw: ("", 0))
    monkeypatch.setattr(loops, "fire_hook", _async_none)
    monkeypatch.setattr(loops, "read_agent_messages", lambda *a, **kw: [])
    monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(loops, "build_cli_command", _fake_cli)
    monkeypatch.setattr(loops, "_accumulate_cost", lambda *a, **kw: 0.0)
    monkeypatch.setattr(loops, "_check_cost_limit", lambda *a, **kw: None)
    monkeypatch.setattr(loops, "_resolve_head_sha", _head_sha)
    monkeypatch.setattr(loops, "_get_task_status", lambda _tid: "in_progress")
    monkeypatch.setattr(loops, "_check_dev_progress",
                        lambda *a, **kw: ("continue", 0, None))
    monkeypatch.setattr(loops, "_is_audit_type_task", lambda *a, **kw: False)
    monkeypatch.setattr(loops, "_capture_git_diff_context", _async_empty)
    monkeypatch.setattr(loops, "dispatch_agent", _fake_dispatch)
    import equipa.dispatch as _dispatch
    monkeypatch.setattr(_dispatch, "is_feature_enabled", lambda *a, **kw: False)
    return calls


class TestTesterOverloadFailsLoudly:
    """S1: before #2994 this returned outcome ``no_tests`` (a success)."""

    def test_tester_529_is_agent_overloaded_not_no_tests(self, monkeypatch, tmp_path):
        developer_ok = {
            "success": True, "result_text": "RESULT: success\nFILES_CHANGED: a.py",
            "num_turns": 3, "cost": 0.0, "duration": 0.0, "errors": [],
            "has_file_changes": True,
        }
        calls = _stub_dev_test_loop(
            monkeypatch, [developer_ok, _overloaded_result()])
        output: list[str] = []

        result, cycle, outcome = asyncio.run(loops.run_dev_test_loop(
            {"id": 999_299_401, "title": "t", "description": "t",
             "role": "developer"},
            project_dir=str(tmp_path), project_context={},
            args=SimpleNamespace(dispatch_config=None), output=output,
        ))

        assert len(calls) == 2, "developer then tester must both be dispatched"
        assert outcome == OVERLOADED_OUTCOME
        assert outcome != "no_tests"
        assert result["success"] is False
        assert any("Tester agent FAILED: model overloaded" in line
                   for line in output), output


# ===========================================================================
# S1 — security reviewer overloaded: the merge gate must fail loudly
# ===========================================================================


def _stale_clean_review(project_dir: Path, task_id: int) -> None:
    """A clean artifact left over from a PRIOR run of the same task."""
    _write_review(project_dir / f"SECURITY-REVIEW-{task_id}.md", low=1)


class TestSecurityReviewerOverloadBlocksMerge:
    def test_parallel_gate_ignores_stale_artifact_when_reviewer_overloaded(
        self, tmp_path,
    ):
        args = _make_args(security_review=True,
                          dispatch_config={"security_review": True})
        patches, merge_calls = _patch_parallel_mode(
            tmp_path, review_writer=lambda *_a: None)
        patches[0] = patch("equipa.dispatch.fetch_tasks_by_ids", return_value=[
            {"id": 100, "project_id": 1, "title": "t1", "description": "d",
             "role": "developer"},
            {"id": 101, "project_id": 1, "title": "t2", "description": "d",
             "role": "developer"},
        ])

        async def overloaded_review(task, project_dir, project_context, args,
                                    output=None, stable_project_dir=None):
            # The reviewer never ran, but a clean artifact from a prior run
            # of the same task is already on the stable path.
            _stale_clean_review(Path(stable_project_dir), task["id"])
            return _overloaded_result()

        patches[5] = patch("equipa.dispatch.run_security_review",
                           side_effect=overloaded_review)

        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], \
                patches[9] as mock_status, patches[10], patches[11], \
                patches[12], patches[13]:
            asyncio.run(run_parallel_tasks([100, 101], args))

        assert merge_calls == [], "overloaded review must never authorise a merge"
        outcomes = {c.args[0]: c.args[1] for c in mock_status.call_args_list}
        assert outcomes == {100: OVERLOADED_OUTCOME, 101: OVERLOADED_OUTCOME}

    def test_single_task_gate_blocks_as_overloaded(self, tmp_path, monkeypatch):
        _stale_clean_review(tmp_path, 4242)
        merge_outcomes: list[str] = []

        async def overloaded_review(*_a, **_kw):
            return _overloaded_result()

        async def fake_changed_files(*_a, **_kw):
            return ["src/app.py"]

        async def fake_post_merge(**kwargs):
            merge_outcomes.append(kwargs["outcome"])
            return ("merged" if kwargs["outcome"] in ("tests_passed", "no_tests")
                    else "skipped")

        monkeypatch.setattr(cli, "run_security_review", overloaded_review)
        monkeypatch.setattr(cli, "get_changed_files_for_branch", fake_changed_files)
        monkeypatch.setattr(cli, "is_security_review_enabled", lambda _a: True)
        monkeypatch.setattr(cli, "_gated_post_merge", fake_post_merge)
        args = SimpleNamespace(dispatch_config={"security_review": True},
                               dev_test=True)

        outcome = asyncio.run(cli._run_security_review_and_gate(
            {"id": 4242}, str(tmp_path), {}, args, "tests_passed"))

        assert outcome == OVERLOADED_OUTCOME
        assert merge_outcomes == [OVERLOADED_OUTCOME]

    def test_run_security_review_logs_overload_loudly(self, tmp_path, monkeypatch):
        async def fake_run_agent(_cmd, timeout=None):
            return _overloaded_result()

        @contextlib.contextmanager
        def fake_cli(*_a, **_kw):
            yield ["claude"]

        monkeypatch.setattr(loops, "run_agent", fake_run_agent)
        monkeypatch.setattr(loops, "build_cli_command", fake_cli)
        monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "p")
        monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
        monkeypatch.setattr(loops, "get_role_model", lambda *a, **kw: CONFIGURED_MODEL)
        output: list[str] = []

        result = asyncio.run(loops.run_security_review(
            {"id": 7, "title": "t", "description": "d"}, str(tmp_path), {},
            SimpleNamespace(dispatch_config=None), output=output))

        assert is_overloaded_result(result)
        assert any("Security review agent FAILED: model overloaded" in line
                   for line in output), output


# ===========================================================================
# S1 — preflight auto-fix, manager planner/evaluator, reflexion
# ===========================================================================


class TestAutofixStopsOnOverload:
    def test_overloaded_debugger_aborts_autofix_loudly(self, monkeypatch):
        roles_dispatched: list[str] = []

        async def fake_autofix(role, *_a, **_kw):
            roles_dispatched.append(role)
            return _overloaded_result(), 0.0

        async def build_still_broken(*_a, **_kw):
            return (False, "python", "still broken")

        monkeypatch.setattr(preflight, "_dispatch_autofix_agent", fake_autofix)
        monkeypatch.setattr(preflight, "preflight_build_check", build_still_broken)
        output: list[str] = []

        fixed, _cost, summary = asyncio.run(preflight._handle_preflight_failure(
            {"id": 1}, "/tmp", {}, "python", "SyntaxError", None, output=output))

        assert fixed is False
        assert summary == OVERLOADED_OUTCOME
        # Pre-#2994 this went on to 2 more debuggers, a planner ("No plan.")
        # and a guided debugger — each a full 529 retry budget.
        assert roles_dispatched == ["debugger"]
        assert any("model overloaded" in line for line in output)

    def test_overloaded_planner_plan_never_reaches_guided_debugger(self, monkeypatch):
        roles_dispatched: list[str] = []

        async def fake_autofix(role, *_a, **_kw):
            roles_dispatched.append(role)
            if role == "planner":
                return _overloaded_result(), 0.0
            return {"success": True, "result_text": "tried"}, 0.0

        async def build_still_broken(*_a, **_kw):
            return (False, "python", "still broken")

        monkeypatch.setattr(preflight, "_dispatch_autofix_agent", fake_autofix)
        monkeypatch.setattr(preflight, "preflight_build_check", build_still_broken)

        _fixed, _cost, summary = asyncio.run(preflight._handle_preflight_failure(
            {"id": 1}, "/tmp", {}, "python", "SyntaxError", None, output=[]))

        assert summary == OVERLOADED_OUTCOME
        assert roles_dispatched[-1] == "planner"

    def test_preflight_constant_matches_agent_runner(self):
        assert preflight.OVERLOADED_OUTCOME == OVERLOADED_OUTCOME


class TestManagerAgentsOverload:
    @pytest.fixture
    def overloaded_manager(self, monkeypatch):
        async def fake_run_agent(_cmd):
            return _overloaded_result()

        @contextlib.contextmanager
        def fake_cli(*_a, **_kw):
            yield ["claude"]

        monkeypatch.setattr(manager, "run_agent", fake_run_agent)
        monkeypatch.setattr(manager, "build_cli_command", fake_cli)
        monkeypatch.setattr(manager, "build_planner_prompt", lambda *a, **kw: "p")
        monkeypatch.setattr(manager, "build_evaluator_prompt", lambda *a, **kw: "p")
        monkeypatch.setattr(manager, "get_role_turns", lambda *a, **kw: 5)
        return SimpleNamespace(model=CONFIGURED_MODEL)

    def test_planner_overload_plans_nothing(self, overloaded_manager):
        output: list[str] = []
        result, task_ids = asyncio.run(manager.run_planner_agent(
            "goal", 1, "/tmp", {}, overloaded_manager, output=output))
        assert task_ids == []
        assert is_overloaded_result(result)
        assert any("model overloaded" in line for line in output)

    def test_evaluator_overload_leaves_goal_blocked(self, overloaded_manager):
        result, parsed = asyncio.run(manager.run_evaluator_agent(
            "goal", 1, "/tmp", {}, [], [], overloaded_manager, output=[]))
        assert is_overloaded_result(result)
        assert parsed["goal_status"] == "blocked"
        assert parsed["blockers"] == OVERLOADED_OUTCOME
        assert parsed["tasks_created"] == []


class TestReflexionOverload:
    def test_overloaded_reflection_is_never_recorded(self, monkeypatch):
        async def fake_run_agent(_cmd, timeout=None):
            return _overloaded_result()

        db_conn = MagicMock(side_effect=AssertionError("must not write"))
        monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)
        monkeypatch.setattr(reflexion, "db_conn", db_conn)
        output: list[str] = []

        asyncio.run(reflexion.run_reflexion_agent(
            {"id": 5, "title": "t", "description": "d"}, "agent output",
            "tests_failed", output=output))

        db_conn.assert_not_called()
        assert any("Reflection agent FAILED: model overloaded" in line
                   for line in output), output


# ===========================================================================
# S1 — RLM decomposition
# ===========================================================================


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestRlmOverload:
    FILES = {"a.py": "print('x')\n"}

    def test_outer_agent_529_marks_session_overloaded(self, monkeypatch):
        monkeypatch.setattr(rlm_decompose.subprocess, "run",
                            lambda *a, **kw: _completed(1, stderr=OVERLOADED_STDERR))
        result = rlm_decompose.run_decompose_session(
            "review", "/tmp", "code-reviewer", repo_files=self.FILES,
            model=CONFIGURED_MODEL)
        assert result.overloaded is True
        assert result.success is False

    def test_sub_query_529_fails_the_session(self, monkeypatch):
        outer_responses = iter([
            "```python\nprint(sub_query('find bugs', repo_files))\n```",
            "Final review: no issues found.",
        ])
        monkeypatch.setattr(rlm_decompose, "_call_outer_agent",
                            lambda **_kw: next(outer_responses))
        monkeypatch.setattr(rlm_decompose.subprocess, "run",
                            lambda *a, **kw: _completed(1, stderr=OVERLOADED_STDERR))

        result = rlm_decompose.run_decompose_session(
            "review", "/tmp", "code-reviewer", repo_files=self.FILES,
            model=CONFIGURED_MODEL)

        # Pre-#2994 the "no issues found" review built on a failed sub-query
        # was a success.
        assert result.overloaded is True
        assert result.success is False
        assert any("overloaded" in err for err in result.errors)

    def test_non_overload_failure_is_not_marked_overloaded(self, monkeypatch):
        monkeypatch.setattr(rlm_decompose.subprocess, "run",
                            lambda *a, **kw: _completed(2, stderr="bad flag"))
        result = rlm_decompose.run_decompose_session(
            "review", "/tmp", "code-reviewer", repo_files=self.FILES,
            model=CONFIGURED_MODEL)
        assert result.overloaded is False
        assert result.success is False

    def test_dispatch_agent_reports_rlm_overload_as_agent_overloaded(
        self, monkeypatch,
    ):
        overloaded = rlm_decompose.DecomposeResult(
            output="", sub_queries_run=1, files_examined=1,
            errors=["x"], overloaded=True)
        monkeypatch.setattr(rlm_decompose, "run_decompose_session",
                            lambda **_kw: overloaded)
        monkeypatch.setattr(rlm_decompose, "load_repo_files", lambda _d: self.FILES)
        monkeypatch.setattr(rlm_decompose, "should_decompose", lambda *a: True)
        monkeypatch.setattr(agent_runner, "is_feature_enabled", lambda *a, **kw: True)

        result = asyncio.run(agent_runner.dispatch_agent(
            ["claude", "--model", CONFIGURED_MODEL], role="code-reviewer",
            output=[], max_turns=5, task_id=1, cycle=1,
            system_prompt="review", project_dir="/tmp",
            args=SimpleNamespace(dispatch_config={}, provider=None)))

        assert is_overloaded_result(result)
        assert result["success"] is False


# ===========================================================================
# S1 — the overloaded outcome is never counted as success
# ===========================================================================


class TestOverloadedOutcomeNeverSuccess:
    def test_update_task_status_maps_overloaded_to_blocked(self, monkeypatch):
        import equipa.db as db

        executed: list[tuple] = []

        class _Conn:
            def execute(self, sql, params=()):
                executed.append((sql, params))
                return SimpleNamespace(fetchone=lambda: {"status": "in_progress"})

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(db, "db_conn", lambda write=False: _Conn())
        db.update_task_status(1, OVERLOADED_OUTCOME, output=[])

        updates = [p for sql, p in executed if sql.lstrip().startswith("UPDATE")]
        assert updates and updates[0][0] == "blocked"

    def test_post_task_telemetry_records_failure(self, monkeypatch):
        recorded: list[tuple[str, bool]] = []

        async def _async_none(*_a, **_kw):
            return None

        monkeypatch.setattr(cli, "update_task_status", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "record_agent_run", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "run_quality_scoring",
                            MagicMock(side_effect=AssertionError("not scored")))
        monkeypatch.setattr(cli, "maybe_run_reflexion", _async_none)
        monkeypatch.setattr(cli, "update_injected_episode_q_values_for_task",
                            lambda *a, **kw: None)
        monkeypatch.setattr(cli, "record_model_outcome",
                            lambda model, ok: recorded.append((model, ok)))

        asyncio.run(cli._post_task_telemetry(
            {"id": 1}, {}, OVERLOADED_OUTCOME, role="developer",
            model=CONFIGURED_MODEL, max_turns=10))

        assert recorded == [(CONFIGURED_MODEL, False)]


# ===========================================================================
# S9 — persistent-retry mode is bounded
# ===========================================================================


class TestPersistentRetryCeiling:
    def _run(self, monkeypatch, stderr: str, **kwargs) -> tuple[dict, int]:
        attempts = {"n": 0}

        async def always_capacity_error(_cmd, **_kw):
            attempts["n"] += 1
            return {"success": False, "errors": [stderr], "result_text": "",
                    "num_turns": 0}

        monkeypatch.setattr(agent_runner, "_run_agent_streaming_impl",
                            always_capacity_error)
        monkeypatch.setattr(agent_runner, "get_retry_delay", lambda *_a, **_k: 0.0)
        result = asyncio.run(agent_runner.run_agent_streaming_with_retry(
            ["claude", "--model", CONFIGURED_MODEL], persistent_retry=True,
            **kwargs))
        return result, attempts["n"]

    def test_sustained_529_fails_loudly_at_ceiling(self, monkeypatch):
        result, attempts = self._run(
            monkeypatch, OVERLOADED_STDERR, persistent_max_attempts=4)
        assert attempts == 4
        assert result["outcome"] == OVERLOADED_OUTCOME
        assert result["success"] is False

    def test_ceiling_read_from_dispatch_config(self, monkeypatch):
        set_active_dispatch_config(
            {"model": CONFIGURED_MODEL, PERSISTENT_RETRY_MAX_ATTEMPTS_KEY: 3})
        result, attempts = self._run(monkeypatch, OVERLOADED_STDERR)
        assert attempts == 3
        assert is_overloaded_result(result)

    def test_rate_limit_ceiling_fails_without_overloaded_outcome(self, monkeypatch):
        result, attempts = self._run(
            monkeypatch, "API Error: 429 rate_limit_error",
            persistent_max_attempts=2)
        assert attempts == 2
        assert result["success"] is False
        assert result.get("outcome") != OVERLOADED_OUTCOME
        assert any("Persistent retry ceiling" in e for e in result["errors"])

    @pytest.mark.parametrize("bad", [0, -1, True, "5", 2.5, None])
    def test_invalid_config_value_falls_back_to_default(self, bad):
        config = {"model": CONFIGURED_MODEL, PERSISTENT_RETRY_MAX_ATTEMPTS_KEY: bad}
        assert (get_persistent_retry_max_attempts(config)
                == DEFAULT_PERSISTENT_RETRY_MAX_ATTEMPTS)

    @pytest.mark.parametrize("bad", [0, -3, True, 1.5])
    def test_invalid_explicit_ceiling_raises(self, bad):
        with pytest.raises(ValueError):
            agent_runner._resolve_persistent_ceiling(bad)


# ===========================================================================
# S2 — model resolution never reads the CWD
# ===========================================================================


class TestModelConfigNeverFromCwd:
    def test_cwd_dispatch_config_is_ignored(self, tmp_path, monkeypatch):
        cwd = tmp_path / "attacker_cwd"
        cwd.mkdir()
        (cwd / "dispatch_config.json").write_text(
            json.dumps({"model": "claude-3-opus-20240229"}))
        monkeypatch.chdir(cwd)

        assert get_configured_model() == CONFIGURED_MODEL
        assert not is_model_allowed("claude-3-opus-20240229")

    def test_registered_orchestrator_config_wins_over_env(self):
        set_active_dispatch_config({"model": "claude-opus-6"})
        assert get_configured_model() == "claude-opus-6"

    def test_without_env_uses_repo_root_file_not_cwd(self, monkeypatch):
        monkeypatch.delenv(DISPATCH_CONFIG_ENV_VAR)
        path = equipa_config.resolve_model_config_path()
        assert path == equipa_config.REPO_DISPATCH_CONFIG_PATH
        assert path.is_absolute()

    def test_set_active_dispatch_config_rejects_non_dict(self):
        with pytest.raises(TypeError):
            set_active_dispatch_config("dispatch_config.json")


# ===========================================================================
# S3 — allowlist, not denylist
# ===========================================================================


class TestModelAllowlist:
    @pytest.mark.parametrize("requested", [
        "claude-opus-4-20250514", "claude-3-opus-20240229", "claude-2.1",
        "sonnet", "haiku", "opus", "claude-sonnet-5", "  ", None, 42,
    ])
    def test_everything_but_configured_resolves_to_configured(self, requested):
        assert resolve_claude_model(requested) == CONFIGURED_MODEL

    def test_configured_model_is_honoured(self):
        assert resolve_claude_model(CONFIGURED_MODEL) == CONFIGURED_MODEL

    def test_approved_upgrade_is_honoured(self):
        config = {"model": CONFIGURED_MODEL,
                  APPROVED_MODEL_UPGRADES_KEY: [APPROVED_UPGRADE]}
        assert resolve_claude_model(APPROVED_UPGRADE, config) == APPROVED_UPGRADE
        assert resolve_claude_model("claude-opus-4-20250514", config) == CONFIGURED_MODEL

    def test_malformed_upgrade_list_never_widens_allowlist(self):
        config = {"model": CONFIGURED_MODEL,
                  APPROVED_MODEL_UPGRADES_KEY: "claude-opus-4-20250514"}
        assert not is_model_allowed("claude-opus-4-20250514", config)

    def test_forgesmith_refuses_non_configured_model_writes(self):
        import forgesmith

        config = {"model": CONFIGURED_MODEL}
        refuse = forgesmith._refuse_dispatch_config_write
        assert refuse("model", "claude-opus-4-20250514", config)
        assert refuse("model_developer", "sonnet", config)
        assert refuse(APPROVED_MODEL_UPGRADES_KEY, ["claude-2.1"], config)
        assert refuse("model", CONFIGURED_MODEL, config) is None
        assert refuse("max_turns", 40, config) is None

    def test_mcp_allowlist_is_exact_configured_ids(self, _isolated_model_config):
        import equipa.mcp_server as mcp_server

        _isolated_model_config.write_text(json.dumps({
            "model": CONFIGURED_MODEL,
            APPROVED_MODEL_UPGRADES_KEY: [APPROVED_UPGRADE, "haiku"],
        }))
        assert mcp_server._allowed_models() == {CONFIGURED_MODEL, APPROVED_UPGRADE}


# ===========================================================================
# S7 — project-role frontmatter model goes through the allowlist
# ===========================================================================


class TestRoleFrontmatterModel:
    @pytest.mark.parametrize("frontmatter_model", [
        "haiku", "claude-opus-4-20250514", "opus",
    ])
    def test_frontmatter_model_resolves_to_configured(
        self, monkeypatch, frontmatter_model,
    ):
        monkeypatch.setattr(roles, "_resolve_role_cfg",
                            lambda *_a, **_kw: SimpleNamespace(model=frontmatter_model))
        args = SimpleNamespace(dispatch_config=None, model=None)
        assert roles.get_role_model("infra-operator", args) == CONFIGURED_MODEL

    def test_frontmatter_configured_model_is_kept(self, monkeypatch):
        monkeypatch.setattr(roles, "_resolve_role_cfg",
                            lambda *_a, **_kw: SimpleNamespace(model=CONFIGURED_MODEL))
        args = SimpleNamespace(dispatch_config=None, model=None)
        assert roles.get_role_model("infra-operator", args) == CONFIGURED_MODEL


# ===========================================================================
# S4 / S5 — autoresearch tiers 2/3
# ===========================================================================


class TestAutoresearchAnthropicCall:
    @pytest.fixture
    def autoresearch(self):
        import autoresearch_prompts
        return autoresearch_prompts

    def test_no_pinned_model_literal(self, autoresearch):
        source = Path(autoresearch.__file__).read_text()
        assert "claude-opus-4-20250514" not in source
        assert not hasattr(autoresearch, "ANTHROPIC_OPUS_MODEL")

    def test_uses_configured_model_as_api_id(self, autoresearch):
        assert autoresearch.configured_anthropic_model() == "claude-opus-5-5"

    @pytest.mark.parametrize("alias", ["opus", "sonnet[1m]", ""])
    def test_alias_is_refused(self, autoresearch, alias):
        with pytest.raises(ValueError):
            autoresearch.to_api_model_id(alias)

    def test_key_only_in_headers_never_in_a_subprocess(
        self, autoresearch, monkeypatch,
    ):
        secret = "sk-ant-test-'quote-breaker"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        monkeypatch.setattr(autoresearch.subprocess, "run",
                            MagicMock(side_effect=AssertionError("no subprocess")))
        sent: dict[str, Any] = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return json.dumps({"stop_reason": "end_turn", "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "NEW PROMPT"},
                ]}).encode()

        def fake_urlopen(request, timeout):
            sent["request"] = request
            return _Response()

        monkeypatch.setattr(autoresearch.urllib.request, "urlopen", fake_urlopen)

        text = autoresearch.call_anthropic("meta prompt")

        request = sent["request"]
        body = json.loads(request.data)
        assert text == "NEW PROMPT"
        assert body["model"] == "claude-opus-5-5"
        assert "temperature" not in body
        assert request.get_header("X-api-key") == secret
        assert secret not in request.data.decode()
        assert secret not in request.full_url

    def test_refusal_returns_empty(self, autoresearch):
        assert autoresearch.extract_response_text(
            {"stop_reason": "refusal", "content": []}) == ""

    def test_max_tokens_truncation_returns_empty(self, autoresearch):
        # A truncated prompt must never be written back as the new role
        # prompt; thinking shares max_tokens, so this is a real failure mode.
        assert autoresearch.extract_response_text({
            "stop_reason": "max_tokens",
            "content": [{"type": "text", "text": "PARTIAL PROMPT"}],
        }) == ""


# ===========================================================================
# S1 — remaining call sites: retry wrapper, single-agent mode, code review
# ===========================================================================


class TestRunAgentWithRetriesOverload:
    def test_overloaded_run_is_returned_without_a_rerun(self, monkeypatch):
        calls: list[list[str]] = []

        async def fake_run_agent(cmd):
            calls.append(cmd)
            return _overloaded_result()

        monkeypatch.setattr(agent_runner, "run_agent", fake_run_agent)

        result, attempts = asyncio.run(agent_runner.run_agent_with_retries(
            ["claude", "--model", CONFIGURED_MODEL], {"id": 1}, 3))

        assert len(calls) == 1, "run_agent already spent its own 529 budget"
        assert attempts == 1
        assert is_overloaded_result(result)


class TestSingleAgentOverload:
    def test_single_agent_529_records_agent_overloaded(
        self, monkeypatch, tmp_path, capsys,
    ):
        recorded: dict[str, Any] = {}

        async def fake_streaming(_cmd, role=None):
            return _overloaded_result()

        async def fake_telemetry(_task, _result, outcome, **_kw):
            recorded["outcome"] = outcome

        @contextlib.contextmanager
        def fake_cli(*_a, **_kw):
            yield ["claude", "--model", CONFIGURED_MODEL]

        import equipa.role_resolver as role_resolver
        monkeypatch.setattr(role_resolver, "is_role_early_term_exempt",
                            lambda *a, **kw: False)
        monkeypatch.setattr(cli, "get_role_turns", lambda *a, **kw: 10)
        monkeypatch.setattr(cli, "get_role_model",
                            lambda *a, **kw: CONFIGURED_MODEL)
        monkeypatch.setattr(cli, "calculate_dynamic_budget",
                            lambda max_turns, effort=None: (max_turns, max_turns))
        monkeypatch.setattr(cli, "build_system_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr(cli, "build_cli_command", fake_cli)
        monkeypatch.setattr(cli, "run_agent_streaming", fake_streaming)
        monkeypatch.setattr(cli, "_post_task_telemetry", fake_telemetry)
        monkeypatch.setattr(cli, "verify_task_updated", lambda _tid: (True, "ok"))
        monkeypatch.setattr(cli, "print_summary", lambda *a, **kw: None)

        asyncio.run(cli._run_single_agent_mode(
            {"id": 999_299_402, "title": "t", "description": "d"},
            str(tmp_path), {},
            SimpleNamespace(role="developer", retries=3, dispatch_config=None),
        ))

        # Before #2994 this was the generic "developer_failed".
        assert recorded["outcome"] == OVERLOADED_OUTCOME
        assert "FAILED: model overloaded (529)" in capsys.readouterr().out


class TestCodeReviewOverload:
    def test_overloaded_code_review_is_never_reported_clean(
        self, monkeypatch, tmp_path,
    ):
        async def fake_run_agent(_cmd, timeout=None):
            return _overloaded_result()

        @contextlib.contextmanager
        def fake_cli(*_a, **_kw):
            yield ["claude", "--model", CONFIGURED_MODEL]

        monkeypatch.setattr(loops, "get_role_turns", lambda *a, **kw: 5)
        monkeypatch.setattr(loops, "get_role_model",
                            lambda *a, **kw: CONFIGURED_MODEL)
        monkeypatch.setattr(loops, "build_system_prompt", lambda *a, **kw: "prompt")
        monkeypatch.setattr(loops, "build_cli_command", fake_cli)
        monkeypatch.setattr(loops, "run_agent", fake_run_agent)
        output: list[str] = []

        result = asyncio.run(loops.run_code_review(
            {"id": 999_299_403, "title": "t", "description": "d",
             "project_id": 23},
            str(tmp_path), {}, SimpleNamespace(dispatch_config=None),
            output=output,
        ))

        assert is_overloaded_result(result)
        assert any("Code review agent FAILED: model overloaded" in line
                   for line in output), output
        assert not any("no Critical or Important findings" in line
                       for line in output)


# ===========================================================================
# S3 — ForgeSmith SIMBA model resolution
# ===========================================================================


class TestSimbaModelAllowlist:
    @pytest.fixture
    def simba(self):
        import forgesmith_simba
        return forgesmith_simba

    def test_older_pinned_model_runs_on_configured(self, simba, monkeypatch):
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            return _completed(1, stderr="stop here")

        monkeypatch.setattr(simba.subprocess, "run", fake_run)

        simba.call_claude_for_rules(
            "prompt", {"simba": {"model": "claude-opus-4-20250514"}})

        cmd = captured["cmd"]
        assert cmd[cmd.index("--model") + 1] == CONFIGURED_MODEL

    def test_standalone_fallback_refuses_instead_of_bare_opus(
        self, simba, monkeypatch,
    ):
        # Make every `from equipa.config import ...` fail, then load a fresh
        # copy of the script so its module-level fallback is exercised.
        monkeypatch.setitem(sys.modules, "equipa.config", None)
        spec = importlib.util.spec_from_file_location(
            "forgesmith_simba_standalone", simba.__file__)
        standalone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standalone)
        run = MagicMock(side_effect=AssertionError("must not call claude"))
        monkeypatch.setattr(standalone.subprocess, "run", run)

        assert standalone.resolve_claude_model("claude-opus-4-20250514") is None
        assert standalone.call_claude_for_rules(
            "prompt", {"simba": {"model": "opus"}}) is None
        run.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
