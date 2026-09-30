"""Task 3140 (fix-forward of 3136): security review findings R3136-01..08.

Copyright 2026 Forgeborn

Each test fails on the 3136 branch as delivered:

* R3136-01: with agent_isolation on, the Ollama provider is refused before
  ``run_ollama_agent`` runs a single model-chosen command, and a fence
  checks every entry point that executes agent-chosen commands;
* R3136-02: the link check covers the committed task-branch tip and the
  clone's HEAD, not only the exported working tree;
* R3136-05: links are resolved through the tree's other links, so a pair
  of individually in-tree links cannot point outside;
* R3136-07: the source fences require the refusal to gate the spawn (an
  ``if`` on its result that returns or raises first), not merely to exist.
"""

from __future__ import annotations

import ast
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from equipa import agent_launcher, isolation

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT = "equipa-agent-1-1-0123456789abcdef"
_GIT = shutil.which("git") or "/usr/bin/git"


def _settings(tmp_path: Path, **overrides) -> isolation.IsolationSettings:
    exchange = tmp_path / "exchange"
    exchange.mkdir(exist_ok=True)
    section = {"exchange_dir": str(exchange), "git_executable": _GIT,
               "python": sys.executable, **overrides}
    return isolation.load_isolation_settings({"agent_isolation": section})


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [_GIT, "-c", "user.name=Orchestrator", "-c",
         "user.email=o@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _flag(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(isolation, "isolation_enabled",
                        lambda config=None: enabled)


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A main checkout on ``main`` and a linked task worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / ".gitignore").write_text(".equipa-artifacts/\n")
    (main / "README").write_text("base\n")
    (main / "docs").mkdir()
    (main / "docs" / "index.md").write_text("docs\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-q", "-m", "base", cwd=main)
    worktree = tmp_path / "worktrees" / "task-1"
    worktree.parent.mkdir()
    _git("worktree", "add", "-q", "-b", "forge-task-1", str(worktree), cwd=main)
    return {"main": main, "worktree": worktree}


def _export_clone(repo: dict[str, Path], tmp_path: Path, prepare) -> tuple:
    """Build the agent's clone as the launcher does, let ``prepare(clone)``
    change it, export it, and return what the import needs."""
    settings = _settings(tmp_path)
    info = isolation.describe_worktree(str(repo["worktree"]))
    bundle = tmp_path / "handoff.bundle"
    handoff = isolation.build_handoff(
        ["claude", "-p", "x"], str(repo["worktree"]),
        {"PATH": os.environ["PATH"]}, settings, UNIT, info, "tok", bundle)
    handoff.header["cgroup"]["path"] = f"/app.slice/{UNIT}.scope"
    session = agent_launcher._IsolatedSession(handoff.header)
    home = tmp_path / "agent-home"
    home.mkdir()
    session.home = str(home)
    with open(bundle, "rb") as source:
        session.receive_workspace(source.fileno(), bundle.stat().st_size)
    prepare(Path(session.repo_dir))
    session.export()
    session.discard()
    return info, handoff, settings


def _import(info, handoff, settings) -> str:
    return isolation.import_agent_export(
        info, UNIT, handoff.export_path, settings.max_export_bytes)


def _commit_in_clone(clone: Path, *paths: str) -> None:
    _git("add", "-f", "--", *paths, cwd=clone)
    _git("commit", "-q", "-m", "agent commit", cwd=clone)


# --- R3136-01: the Ollama provider under isolation -------------------------------------


@pytest.fixture
def ollama_calls(monkeypatch) -> list[dict]:
    """Record run_ollama_agent calls instead of talking to a model."""
    import ollama_agent

    calls: list[dict] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {"success": True, "result_text": "RESULT: success",
                "errors": [], "num_turns": 1, "duration": 0.0, "cost": 0.0}

    monkeypatch.setattr(ollama_agent, "run_ollama_agent", fake_run)
    return calls


def _dispatch(tmp_path: Path, monkeypatch, **args) -> dict:
    from equipa import agent_runner

    monkeypatch.setattr(agent_runner, "verify_skill_integrity", lambda: True)
    namespace = SimpleNamespace(**{"provider": None, "dispatch_config": None,
                                   **args})
    return asyncio.run(agent_runner.dispatch_agent(
        ["claude", "-p", "x"], "developer", None, 5, 1, 1,
        system_prompt="system", project_dir=str(tmp_path), args=namespace))


@pytest.mark.parametrize("args", [
    {"provider": "ollama"},
    {"dispatch_config": {"provider": "ollama"}},
    {"dispatch_config": {"provider_developer": "ollama"}},
])
def test_ollama_provider_is_refused_with_isolation_on(
        tmp_path: Path, monkeypatch, ollama_calls, args: dict) -> None:
    _flag(monkeypatch, True)
    result = _dispatch(tmp_path, monkeypatch, **args)
    assert ollama_calls == []
    assert result["success"] is False and result["result"] == "blocked"
    assert "RESULT: blocked" in result["result_text"]
    assert "Ollama agent refused: agent_isolation is on" in result["errors"][0]


def test_ollama_provider_unchanged_with_isolation_off(
        tmp_path: Path, monkeypatch, ollama_calls) -> None:
    _flag(monkeypatch, False)
    result = _dispatch(tmp_path, monkeypatch, provider="ollama")
    assert result["success"] is True
    assert [call["role"] for call in ollama_calls] == ["developer"]


def test_run_ollama_agent_itself_refuses_with_isolation_on(
        tmp_path: Path, monkeypatch) -> None:
    """Backstop for direct callers (the module's own ``__main__``): no model
    request and no shell command once the flag is on."""
    import ollama_agent

    _flag(monkeypatch, True)

    def no_call(*args, **kwargs):
        raise AssertionError("the Ollama agent ran with agent_isolation on")

    monkeypatch.setattr(ollama_agent, "ollama_chat", no_call)
    monkeypatch.setattr(ollama_agent.subprocess, "run", no_call)
    result = ollama_agent.run_ollama_agent("system", str(tmp_path))
    assert result["success"] is False and result["result"] == "blocked"
    assert "agent_isolation is on" in result["errors"][0]


def test_run_ollama_agent_unchanged_with_isolation_off(
        tmp_path: Path, monkeypatch) -> None:
    import ollama_agent

    _flag(monkeypatch, False)
    requests: list = []

    def chat(*args, **kwargs):
        requests.append(args)
        return {"message": {"role": "assistant", "content": "RESULT: success"}}

    monkeypatch.setattr(ollama_agent, "ollama_chat", chat)
    result = ollama_agent.run_ollama_agent("system", str(tmp_path), max_turns=3)
    assert len(requests) == 1 and "blocked" not in result.get("result", "")
    assert result["result_text"] == "RESULT: success"


# --- Source fences: the refusal must gate the entry point (R3136-01, R3136-07) ----------


def _contains(node: ast.AST, target: ast.AST) -> bool:
    return any(child is target for child in ast.walk(node))


def _callee(call: ast.Call) -> str | None:
    return getattr(call.func, "id", getattr(call.func, "attr", None))


def _refusal_names(scope: ast.AST, refusal: str) -> set[str]:
    """Variables assigned the result of a ``refusal`` call in ``scope``."""
    names = set()
    for node in ast.walk(scope):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and _callee(node.value) == refusal):
            names.update(target.id for target in node.targets
                         if isinstance(target, ast.Name))
    return names


def _is_gate(statement: ast.stmt, refusal: str, names: set[str]) -> bool:
    """``if <refusal result>: ... return/raise`` at the top of the body."""
    if not isinstance(statement, ast.If):
        return False
    tested = any((isinstance(node, ast.Name) and node.id in names)
                 or (isinstance(node, ast.Call) and _callee(node) == refusal)
                 for node in ast.walk(statement.test))
    exits = any(isinstance(node, (ast.Return, ast.Raise))
                for node in statement.body)
    return tested and exits


def _child_blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    blocks = [getattr(statement, field) for field in ("body", "orelse", "finalbody")
              if isinstance(getattr(statement, field, None), list)]
    blocks += [handler.body for handler in getattr(statement, "handlers", [])]
    return blocks


def _gated(scope_body: list[ast.stmt], target: ast.AST, refusal: str,
           names: set[str]) -> bool:
    """Is ``target`` only reachable after a refusal gate? Walks the chain
    of blocks that contain it; a gate in any enclosing block, before the
    statement leading to ``target``, dominates it."""
    block = scope_body
    while block:
        for index, statement in enumerate(block):
            if not _contains(statement, target):
                continue
            if any(_is_gate(earlier, refusal, names) for earlier in block[:index]):
                return True
            block = next((child for child in _child_blocks(statement)
                          if any(_contains(s, target) for s in child)), [])
            break
        else:
            return False
    return False


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _python_sources() -> list[Path]:
    return [*sorted((REPO_ROOT / "equipa").rglob("*.py")),
            *sorted((REPO_ROOT / "scripts").glob("*.py")),
            *sorted(REPO_ROOT.glob("*.py"))]


def test_gate_detection_needs_an_exit_before_the_spawn() -> None:
    """The fence itself: a refusal that is computed, or only logged, does
    not count; one tested by an ``if`` that returns first does."""
    cases = {
        "ignored": ("def f():\n    refusal = unisolated_spawn_refusal('x')\n"
                    "    spawn()\n", False),
        "logged": ("def f():\n    refusal = unisolated_spawn_refusal('x')\n"
                   "    if refusal:\n        log(refusal)\n    spawn()\n", False),
        "after": ("def f():\n    spawn()\n    refusal = unisolated_spawn_refusal('x')\n"
                  "    if refusal:\n        return None\n", False),
        "gated": ("def f():\n    refusal = unisolated_spawn_refusal('x')\n"
                  "    if refusal:\n        log(refusal)\n        return None\n"
                  "    try:\n        spawn()\n    finally:\n        pass\n", True),
        "raises": ("def f():\n    if unisolated_spawn_refusal('x'):\n"
                   "        raise RuntimeError\n    spawn()\n", True),
    }
    for name, (source, expected) in cases.items():
        function = next(_functions(ast.parse(source)))
        spawn = next(node for node in ast.walk(function)
                     if isinstance(node, ast.Call) and _callee(node) == "spawn")
        names = _refusal_names(function, "unisolated_spawn_refusal")
        assert _gated(function.body, spawn, "unisolated_spawn_refusal",
                      names) is expected, name


def test_every_run_ollama_agent_call_is_gated_by_the_refusal() -> None:
    """R3136-01 fence: a call of ``run_ollama_agent`` inside a function
    must sit behind an ``unisolated_spawn_refusal`` gate."""
    found, ungated = [], []
    for path in _python_sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for function in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            names = _refusal_names(function, "unisolated_spawn_refusal")
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and _callee(node) == "run_ollama_agent":
                    found.append(f"{relative}:{function.name}")
                    if not _gated(function.body, node,
                                  "unisolated_spawn_refusal", names):
                        ungated.append(f"{relative}:{node.lineno}")
    assert "equipa/agent_runner.py:dispatch_agent" in found
    assert not ungated, f"run_ollama_agent reachable unrefused: {ungated}"


def test_run_ollama_agent_refuses_before_its_tool_loop() -> None:
    """The model-driven loop (and so every tool execution) of
    run_ollama_agent sits behind the refusal, which covers module-level
    callers such as its ``__main__`` demo."""
    tree = ast.parse((REPO_ROOT / "ollama_agent.py").read_text(encoding="utf-8"))
    function = next(f for f in _functions(tree) if f.name == "run_ollama_agent")
    loop = next(node for node in function.body if isinstance(node, ast.While))
    names = _refusal_names(function, "unisolated_spawn_refusal")
    assert _gated(function.body, loop, "unisolated_spawn_refusal", names)


# Every place that runs a command string or code it did not write itself,
# with why it is safe under agent_isolation. A new site fails the fence.
_COMMAND_EXECUTORS = {
    # Operator-configured lifecycle hooks, never agent text.
    "equipa/hooks/__init__.py": "operator hooks",
    # REPL over code from the outer CLI call, which refuses first.
    "equipa/rlm_decompose.py": "RLM REPL (outer agent refused)",
    # Reached only through run_ollama_agent's tool table (gated above).
    "ollama_agent.py": "Ollama tools",
}


def _executes_commands(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        shell = any(keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True for keyword in node.keywords)
        os_system = (isinstance(node.func, ast.Attribute) and node.func.attr == "system"
                     and isinstance(node.func.value, ast.Name) and node.func.value.id == "os")
        if (shell or os_system or _callee(node) == "create_subprocess_shell"
                or (isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval"))):
            lines.append(node.lineno)
    return lines


def test_every_command_executing_entry_point_is_known() -> None:
    """R3136-01 fence: shell=True, create_subprocess_shell, os.system,
    exec and eval appear only where the reason above holds."""
    unknown = []
    for path in _python_sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        lines = _executes_commands(ast.parse(path.read_text(encoding="utf-8")))
        if lines and relative not in _COMMAND_EXECUTORS:
            unknown.append(f"{relative}:{lines}")
    assert not unknown, f"new command-executing entry points: {unknown}"


def test_ollama_tool_execution_is_only_reached_through_run_ollama_agent() -> None:
    tree = ast.parse((REPO_ROOT / "ollama_agent.py").read_text(encoding="utf-8"))
    callers = {function.name for function in _functions(tree)
               for node in ast.walk(function)
               if isinstance(node, ast.Call) and _callee(node) == "exec_bash"}
    # exec_bash is called from lambdas in the module-level tool table only.
    assert callers == set()
    table_users = {function.name for function in _functions(tree)
                   for node in ast.walk(function)
                   if isinstance(node, ast.Name) and node.id == "TOOL_HANDLERS"}
    assert table_users == {"run_ollama_agent"}


# --- R3136-07: stronger versions of the 3136 source fences -----------------------------

_SPAWN_CALLS = {
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
    ("os", "system"), ("os", "posix_spawn"), ("os", "posix_spawnp"),
    ("os", "popen"), ("os", "execv"), ("os", "execve"), ("os", "execvp"),
}


def test_every_preflight_spawn_is_gated_by_the_refusal() -> None:
    tree = ast.parse((REPO_ROOT / "equipa" / "preflight.py").read_text())
    sites = []
    for function in _functions(tree):
        names = _refusal_names(function, "worktree_execution_refusal")
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and (node.func.value.id, node.func.attr) in _SPAWN_CALLS):
                sites.append((function.name, node.lineno,
                              _gated(function.body, node,
                                     "worktree_execution_refusal", names)))
    assert len(sites) >= 2
    assert [site for site in sites if not site[2]] == []


_THROUGH_RUN_AGENT = {"equipa/agent_runner.py", "equipa/reflexion.py"}


def _names_claude_cli(function: ast.AST) -> bool:
    """A ``claude -p/--print`` argv literal, or a shell string (plain or
    f-string) that starts with ``claude `` (R3136-06)."""
    for node in ast.walk(function):
        if isinstance(node, ast.List) and node.elts:
            first = node.elts[0]
            prints = any(isinstance(elt, ast.Constant)
                         and elt.value in ("-p", "--print") for elt in node.elts)
            if prints and ((isinstance(first, ast.Constant) and first.value == "claude")
                           or (isinstance(first, ast.Name)
                               and first.id == "claude_bin")):
                return True
        if isinstance(node, ast.JoinedStr) and node.values:
            node = node.values[0]
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.startswith("claude ")):
            return True
    return False


def _process_spawns(function: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in _SPAWN_CALLS]


def test_every_direct_claude_spawn_is_gated_by_the_refusal() -> None:
    """ISO-06 / R3136-06 / R3136-07: in a function that names the Claude
    CLI, every process spawn sits behind an unisolated_spawn_refusal gate."""
    found, ungated = [], []
    for path in _python_sources():
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in _THROUGH_RUN_AGENT:
            continue
        for function in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            spawns = _process_spawns(function)
            if not spawns or not _names_claude_cli(function):
                continue
            found.append(f"{relative}:{function.name}")
            names = _refusal_names(function, "unisolated_spawn_refusal")
            ungated += [f"{relative}:{call.lineno} in {function.name}"
                        for call in spawns
                        if not _gated(function.body, call,
                                      "unisolated_spawn_refusal", names)]
    for expected in ("forgesmith.py:dispatch_ghost_scout",
                     "forgesmith.py:call_claude_for_proposals",
                     "scripts/forgesmith_simba.py:call_claude_for_rules",
                     "scripts/autoresearch_loop.py:mutate_prompt",
                     "equipa/rlm_decompose.py:_call_outer_agent",
                     "equipa/rlm_decompose.py:_run_sub_query"):
        assert expected in found, f"the fence no longer sees {expected}"
    assert not ungated, f"claude spawns not gated by the refusal: {ungated}"


def test_claude_fence_sees_a_shell_string_spawn() -> None:
    function = next(_functions(ast.parse(
        "def f(tmp):\n"
        "    subprocess.run(['bash', '-c', f'claude --print < {tmp}'])\n")))
    assert _names_claude_cli(function) and _process_spawns(function)


# --- R3136-06: autoresearch prompt mutation ----------------------------------------------


def _autoresearch(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts"))
    import autoresearch_loop

    monkeypatch.setattr(autoresearch_loop, "is_on_claudinator", lambda: True)
    return autoresearch_loop


def test_autoresearch_mutation_refuses_with_isolation_on(monkeypatch) -> None:
    autoresearch = _autoresearch(monkeypatch)
    _flag(monkeypatch, True)
    calls: list = []

    def run(cmd, **kwargs):
        calls.append(cmd)  # mutate_prompt swallows exceptions, so record
        return subprocess.CompletedProcess(cmd, 0, "new prompt", "")

    monkeypatch.setattr(autoresearch.subprocess, "run", run)
    assert autoresearch.mutate_prompt("developer", "prompt", "failures",
                                      {"total": 1}) == ""
    assert calls == []


def test_autoresearch_mutation_runs_the_cli_without_a_shell(monkeypatch) -> None:
    autoresearch = _autoresearch(monkeypatch)
    _flag(monkeypatch, False)
    calls: list = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs["stdin"].read()))
        return subprocess.CompletedProcess(cmd, 0, "new prompt", "")

    monkeypatch.setattr(autoresearch.subprocess, "run", run)
    result = autoresearch.mutate_prompt("developer", "prompt", "failures",
                                        {"total": 1})
    assert result == "new prompt"
    (argv, stdin_text), = calls
    assert argv[:4] == ["claude", "--print", "--model", "opus"]
    assert "--setting-sources" in argv and "--strict-mcp-config" in argv
    assert "bash" not in argv and "failures" in stdin_text


# --- R3136-02: committed links are checked, not only the working tree ------------------


def test_import_refuses_a_committed_link_the_working_tree_dropped(
        repo: dict[str, Path], tmp_path: Path) -> None:
    worktree = repo["worktree"]
    base = _git("rev-parse", "HEAD", cwd=worktree)
    outside = tmp_path / "operator-notes"
    outside.mkdir()

    def plant(clone: Path) -> None:
        (clone / ".equipa-artifacts").symlink_to(str(outside))
        _commit_in_clone(clone, ".equipa-artifacts")
        (clone / ".equipa-artifacts").unlink()

    with pytest.raises(isolation.AgentIsolationError,
                       match=r"committed task-branch tip\) makes "
                             r"\.equipa-artifacts a symbolic link"):
        _import(*_export_clone(repo, tmp_path, plant))
    assert _git("rev-parse", "forge-task-1", cwd=repo["main"]) == base
    assert not (worktree / ".equipa-artifacts").is_symlink()


def test_import_refuses_a_committed_escaping_link(
        repo: dict[str, Path], tmp_path: Path) -> None:
    def plant(clone: Path) -> None:
        (clone / "docs" / "etc").symlink_to("/etc")
        _commit_in_clone(clone, "docs/etc")
        (clone / "docs" / "etc").unlink()

    with pytest.raises(isolation.AgentIsolationError,
                       match="task-branch tip.*outside the worktree"):
        _import(*_export_clone(repo, tmp_path, plant))


def test_import_refuses_a_link_in_the_clones_detached_head(
        repo: dict[str, Path], tmp_path: Path) -> None:
    """The second parent (HEAD when it differs from the branch tip)."""
    def plant(clone: Path) -> None:
        _git("checkout", "-q", "--detach", cwd=clone)
        (clone / "escape").symlink_to("../../..")
        _commit_in_clone(clone, "escape")
        (clone / "escape").unlink()

    with pytest.raises(isolation.AgentIsolationError,
                       match=r"HEAD commit\) adds a symbolic link escape"):
        _import(*_export_clone(repo, tmp_path, plant))


def test_import_accepts_a_committed_in_tree_link(
        repo: dict[str, Path], tmp_path: Path) -> None:
    worktree = repo["worktree"]

    def plant(clone: Path) -> None:
        (clone / "docs" / "latest").symlink_to("index.md")
        _commit_in_clone(clone, "docs/latest")

    tip = _import(*_export_clone(repo, tmp_path, plant))
    assert _git("rev-parse", "forge-task-1", cwd=repo["main"]) == tip
    assert os.readlink(worktree / "docs" / "latest") == "index.md"


# --- R3136-05: links are resolved, not normalised ---------------------------------------


def test_link_escape_follows_links_of_the_tree() -> None:
    links = {"sub/a": "..", "x": "y", "y": "x", "docs/up": "..",
             "abs": "/usr/bin/python3"}
    lookup = links.get
    # sub/b -> a/.. is sub/ lexically, but a is the tree root, so ".." leaves it.
    assert not isolation._link_escapes("sub/b", "a/..")
    assert isolation._link_escapes("sub/b", "a/..", lookup)
    assert isolation._link_escapes("sub/b", "a/../..", lookup)
    assert isolation._link_escapes("z", "x", lookup)          # a link loop
    assert isolation._link_escapes("new", "abs", lookup)      # via an absolute link
    assert not isolation._link_escapes("sub/c", "a/docs", lookup)
    assert not isolation._link_escapes("docs/b", "up/README", lookup)
    assert isolation._link_escapes("docs/b", "up/..", lookup)


def test_import_refuses_a_link_pair_that_resolves_outside(
        repo: dict[str, Path], tmp_path: Path) -> None:
    def plant(clone: Path) -> None:
        (clone / "sub").mkdir()
        (clone / "sub" / "a").symlink_to("..")
        (clone / "sub" / "b").symlink_to("a/..")

    with pytest.raises(isolation.AgentIsolationError,
                       match="sub/b -> a/.. that points outside the worktree"):
        _import(*_export_clone(repo, tmp_path, plant))


def test_import_accepts_a_link_pair_that_stays_inside(
        repo: dict[str, Path], tmp_path: Path) -> None:
    worktree = repo["worktree"]

    def plant(clone: Path) -> None:
        (clone / "sub").mkdir()
        (clone / "sub" / "a").symlink_to("../docs")
        (clone / "sub" / "b").symlink_to("a/../README")

    _import(*_export_clone(repo, tmp_path, plant))
    assert (worktree / "sub" / "b").read_text() == "base\n"


# --- R3136-03: reviewer units never overlap another isolated unit ----------------------


@pytest.fixture
def slots(tmp_path: Path, monkeypatch):
    """acquire_unit_slot on a private lock directory, fast polling, and no
    live agent scopes unless a test adds some."""
    lock_dir = tmp_path / "run"
    lock_dir.mkdir(mode=0o700)
    scopes: list[str] = []
    monkeypatch.setattr(isolation, "_SLOT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(isolation, "live_agent_scopes",
                        lambda app_slice=None: list(scopes))
    monkeypatch.setattr(isolation, "sweep_stale_scopes",
                        lambda app_slice=None: [])

    def acquire(label: str, exclusive: bool, timeout: float = 5.0):
        return isolation.acquire_unit_slot(label, exclusive=exclusive,
                                           timeout=timeout,
                                           lock_dir=str(lock_dir))

    acquire.scopes = scopes
    acquire.lock_dir = lock_dir
    return acquire


async def _pending(awaitable, seconds: float = 0.2):
    """Start ``awaitable`` and give it ``seconds`` to finish; return its task."""
    task = asyncio.ensure_future(awaitable)
    await asyncio.sleep(seconds)
    return task


def test_ordinary_units_run_side_by_side(slots) -> None:
    async def scenario():
        first = await slots("developer 1", False)
        second = await asyncio.wait_for(slots("tester 2", False), 1)
        assert first.held and second.held
        first.release()
        second.release()

    asyncio.run(scenario())


def test_reviewer_waits_for_running_units_and_blocks_new_ones(
        slots, caplog) -> None:
    caplog.set_level("INFO", logger="equipa.isolation")
    order: list[str] = []

    async def scenario():
        developer = await slots("developer unit A", False)
        reviewer_task = await _pending(slots("security-reviewer unit R", True))
        assert not reviewer_task.done()           # waits for the developer
        late_task = await _pending(slots("developer unit B", False))
        assert not late_task.done()               # held back by the waiting reviewer
        order.append("developer A ends")
        developer.release()
        reviewer = await asyncio.wait_for(reviewer_task, 2)
        order.append("reviewer runs")
        await asyncio.sleep(0.2)
        assert not late_task.done()               # still held back while it runs
        reviewer.release()
        late = await asyncio.wait_for(late_task, 2)
        order.append("developer B runs")
        late.release()

    asyncio.run(scenario())
    assert order == ["developer A ends", "reviewer runs", "developer B runs"]
    text = caplog.text
    assert "security-reviewer unit R waits for the running isolated agents" in text
    assert "security-reviewer unit R runs alone" in text
    assert "developer unit B waits for a reviewer" in text
    assert "security-reviewer unit R has ended" in text


def test_reviewers_do_not_overlap_each_other(slots) -> None:
    async def scenario():
        first = await slots("code-reviewer 1", True)
        second_task = await _pending(slots("security-reviewer 2", True))
        assert not second_task.done()
        first.release()
        (await asyncio.wait_for(second_task, 2)).release()

    asyncio.run(scenario())


def test_reviewer_waits_for_live_agent_scopes(slots) -> None:
    """A unit that survived cgroup.kill, or one of an orchestrator without
    the lock, still counts as running."""
    slots.scopes.append("equipa-agent-9-9-00.scope")

    async def scenario():
        task = await _pending(slots("security-reviewer 1", True))
        assert not task.done()
        slots.scopes.clear()
        (await asyncio.wait_for(task, 2)).release()

    asyncio.run(scenario())


def test_wait_past_the_timeout_refuses_and_holds_nothing(slots) -> None:
    async def scenario():
        developer = await slots("developer 1", False)
        with pytest.raises(isolation.AgentIsolationError,
                           match="unit_wait_timeout_sec"):
            await slots("security-reviewer 2", True, timeout=0.2)
        developer.release()
        # The refused reviewer left neither lock behind.
        (await asyncio.wait_for(slots("code-reviewer 3", True), 1)).release()

    asyncio.run(scenario())


_HOLD_SHARED = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_SH)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


def test_units_in_another_process_are_waited_for(slots) -> None:
    """The lock is per user, not per event loop: a unit held by another
    orchestrator process delays a reviewer here."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SHARED,
         str(slots.lock_dir / isolation._UNITS_LOCK_NAME)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"

        async def scenario():
            task = await _pending(slots("security-reviewer 1", True))
            assert not task.done()
            holder.stdin.close()
            (await asyncio.wait_for(task, 5)).release()

        asyncio.run(scenario())
    finally:
        holder.kill()
        holder.wait()


def test_unit_lock_must_be_a_private_regular_file(slots, tmp_path: Path) -> None:
    (slots.lock_dir / isolation._TURNSTILE_LOCK_NAME).symlink_to(tmp_path / "x")
    with pytest.raises(isolation.AgentIsolationError, match="unit lock"):
        asyncio.run(slots("developer 1", False))


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.stdin = None
        self.returncode = 0


def _fake_setup(tmp_path: Path, monkeypatch, lock_dir: Path) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    monkeypatch.setattr(isolation, "load_isolation_settings",
                        lambda config: settings)
    monkeypatch.setattr(isolation, "resolve_agent_identity", lambda s: None)
    monkeypatch.setattr(isolation, "check_host", lambda s, i: None)
    monkeypatch.setattr(isolation, "_runtime_dir", lambda: str(lock_dir))


async def _spawn_as(role: str | None):
    with isolation.unit_role(role):
        return await isolation.spawn_isolated_agent(["claude"], None, {}, {})


def test_spawn_holds_the_slot_until_the_agent_is_released(
        tmp_path: Path, monkeypatch, slots) -> None:
    """spawn_isolated_agent (setup faked) takes the unit's slot by role and
    the agent handle gives it back on release."""
    _fake_setup(tmp_path, monkeypatch, slots.lock_dir)
    started: list[str] = []

    async def fake_spawn(cmd, cwd, env, settings, unit, slot, limit):
        started.append(isolation.current_unit_role() or "agent")
        agent = isolation.IsolatedAgent(_FakeProcess(), unit, settings, None,
                                        None, slot)
        return agent.process, agent

    monkeypatch.setattr(isolation, "_spawn_in_slot", fake_spawn)

    async def scenario():
        _process, developer = await _spawn_as("developer")
        reviewer_task = await _pending(_spawn_as("security-reviewer"))
        assert started == ["developer"] and not reviewer_task.done()
        developer.release()
        _process, reviewer = await asyncio.wait_for(reviewer_task, 2)
        assert started == ["developer", "security-reviewer"]
        tester_task = await _pending(_spawn_as("tester"))
        assert not tester_task.done()
        reviewer.release()
        _process, tester = await asyncio.wait_for(tester_task, 2)
        tester.release()
        assert started == ["developer", "security-reviewer", "tester"]

    asyncio.run(scenario())


def test_failed_setup_gives_the_slot_back(tmp_path: Path, monkeypatch,
                                          slots) -> None:
    _fake_setup(tmp_path, monkeypatch, slots.lock_dir)

    async def broken(*args):
        raise isolation.AgentIsolationError("setup failed")

    monkeypatch.setattr(isolation, "_spawn_in_slot", broken)

    async def scenario():
        with pytest.raises(isolation.AgentIsolationError, match="setup failed"):
            await _spawn_as("developer")
        (await asyncio.wait_for(slots("security-reviewer 1", True), 1)).release()

    asyncio.run(scenario())


class _Handoff:
    export_path = None


class _LiveProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.returncode = None


def test_reviewer_spawn_waits_for_a_running_developer_unit(
        tmp_path: Path, monkeypatch) -> None:
    """End to end through build_cli_command and spawn_isolated_agent, with
    only the launch itself faked: the security reviewer's agent does not
    start while a developer's isolated agent is still running."""
    from equipa import agent_runner

    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    app_slice = tmp_path / "app.slice"
    app_slice.mkdir()
    _fake_setup(tmp_path, monkeypatch, run_dir)
    monkeypatch.setattr(isolation, "user_app_slice", lambda: app_slice)
    monkeypatch.setattr(isolation, "resolve_oauth_token", lambda s: "tok")
    monkeypatch.setattr(isolation, "build_handoff", lambda *a: _Handoff())
    monkeypatch.setattr(isolation, "_SLOT_POLL_SECONDS", 0.01, raising=False)
    launched: list[str] = []

    async def fake_exec(*argv, **kwargs):
        launched.append(f"launch {len(launched) + 1}")
        return _LiveProcess()

    async def established(self, handoff):
        self.started = True

    monkeypatch.setattr(isolation.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(isolation.IsolatedAgent, "establish", established)

    async def spawn(role: str):
        with agent_runner.build_cli_command("prompt", str(tmp_path), 5, "opus",
                                            role=role,
                                            dispatch_config={"x": 1}) as cmd:
            return await isolation.spawn_isolated_agent(cmd, None, {}, {})

    async def scenario():
        _process, developer = await spawn("developer")
        reviewer_task = await _pending(spawn("security-reviewer"), 0.3)
        overlapped = reviewer_task.done()
        developer.release()
        _process, reviewer = await asyncio.wait_for(reviewer_task, 2)
        reviewer.release()
        return overlapped

    assert asyncio.run(scenario()) is False, \
        "the reviewer's agent started while the developer's was running"
    assert launched == ["launch 1", "launch 2"]


def test_build_cli_command_marks_the_role_for_the_isolated_spawn(
        tmp_path: Path) -> None:
    from equipa import agent_runner

    assert isolation.current_unit_role() is None
    with agent_runner.build_cli_command("prompt", str(tmp_path), 5, "opus",
                                        role="security-reviewer",
                                        dispatch_config={"x": 1}):
        assert isolation.current_unit_role() == "security-reviewer"
    assert isolation.current_unit_role() is None


def test_reviewer_spawn_sites_build_their_command_with_the_role() -> None:
    """loops.py runs both reviewers through build_cli_command with their
    role, which is what makes their units exclusive."""
    tree = ast.parse((REPO_ROOT / "equipa" / "loops.py").read_text(encoding="utf-8"))
    roles = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node) == "build_cli_command":
            roles.update(keyword.value.value for keyword in node.keywords
                         if keyword.arg == "role"
                         and isinstance(keyword.value, ast.Constant))
    assert isolation.EXCLUSIVE_ROLES <= roles


def test_unit_wait_timeout_setting(tmp_path: Path) -> None:
    assert _settings(tmp_path).unit_wait_timeout_sec == 21600.0
    assert _settings(tmp_path, unit_wait_timeout_sec=600).unit_wait_timeout_sec == 600.0
    with pytest.raises(isolation.AgentIsolationError,
                       match="unit_wait_timeout_sec"):
        _settings(tmp_path, unit_wait_timeout_sec=1)


# --- R3136-04: nothing may outlive the unit (cron, at, lingering) ----------------------

_CRON_DENIED = "You (equipa-agent) are not allowed to use this program (crontab)"
_AT_DENIED = "You do not have permission to use at."


def _fake_tool(directory: Path, name: str, output: str, status: int) -> str:
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{output}' >&2\nexit {status}\n")
    path.chmod(0o755)
    return str(path)


def _launcher_session(tmp_path: Path, monkeypatch, crontab: tuple[str, int],
                      at: tuple[str, int] | None) -> agent_launcher._IsolatedSession:
    tools = tmp_path / "tools"
    schedulers = [("crontab", (_fake_tool(tools, "crontab", *crontab),))]
    if at is not None:
        schedulers.append(("at", (_fake_tool(tools, "at", *at),)))
    linger = tmp_path / "linger"
    linger.mkdir(exist_ok=True)
    # raising=False: the same fakes show the pre-fix launcher accepting it.
    monkeypatch.setattr(agent_launcher, "_SCHEDULERS", tuple(schedulers),
                        raising=False)
    monkeypatch.setattr(agent_launcher, "_LINGER_DIR", str(linger), raising=False)
    unit = "equipa-agent-1-1-00000000000000a4"
    header = {
        "unit": unit, "argv": ["claude", "-p", "x"], "executable": sys.executable,
        "env": {"PATH": os.environ["PATH"]}, "files": [], "workdir_sources": [],
        "identity": {"user": "equipa-agent", "orchestrator_uid": os.getuid() + 1,
                     "privileged_groups": []},
        "cgroup": {"path": f"/app.slice/{unit}.scope", "pids_max": 64,
                   "memory_max": 256 * 1024 ** 2, "cpu_weight": 100},
        "deny_read": [], "deny_write": [], "must_execute": [], "must_read": [],
        "git": {"executable": _GIT, "hardening_args": [], "hardening_env": {},
                "user_name": "Forgeborn", "user_email": "forgeborn@example.invalid"},
        "workspace": None, "grace": 1.0,
    }
    return agent_launcher._IsolatedSession(header)


def test_launcher_refuses_an_agent_that_may_use_cron(tmp_path, monkeypatch) -> None:
    session = _launcher_session(tmp_path, monkeypatch,
                                ("no crontab for equipa-agent", 1), None)
    with pytest.raises(agent_launcher.IsolationRefused,
                       match="may use crontab.*cron.deny"):
        session._verify_no_scheduler()


def test_launcher_refuses_an_agent_that_may_use_at(tmp_path, monkeypatch) -> None:
    session = _launcher_session(tmp_path, monkeypatch, (_CRON_DENIED, 1), ("", 0))
    with pytest.raises(agent_launcher.IsolationRefused, match="may use at"):
        session._verify_no_scheduler()


def test_launcher_refuses_a_lingering_agent_user(tmp_path, monkeypatch) -> None:
    session = _launcher_session(tmp_path, monkeypatch, (_CRON_DENIED, 1),
                                (_AT_DENIED, 1))
    (tmp_path / "linger" / "equipa-agent").write_text("")
    with pytest.raises(agent_launcher.IsolationRefused, match="lingering"):
        session._verify_no_scheduler()


def test_launcher_accepts_denied_cron_and_at(tmp_path, monkeypatch) -> None:
    session = _launcher_session(tmp_path, monkeypatch, (_CRON_DENIED, 1),
                                (_AT_DENIED, 1))
    session._verify_no_scheduler()


def test_launcher_verify_refuses_an_agent_that_may_use_cron(
        tmp_path, monkeypatch) -> None:
    """verify() itself runs the check (the other inside checks, which need
    a second user and a real cgroup, pass here)."""
    session = _launcher_session(tmp_path, monkeypatch,
                                ("no crontab for equipa-agent", 1), None)
    for name in ("_verify_identity", "_verify_cgroup", "_verify_denied_access",
                 "_verify_required_access"):
        monkeypatch.setattr(session, name, lambda: None)
    with pytest.raises(agent_launcher.IsolationRefused, match="may use crontab"):
        session.verify()


VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_agent_isolation.sh"


def _run_inside_with(tmp_path: Path, tools: dict[str, tuple[str, int]],
                     linger: str = "no") -> list[str]:
    fake_bin = tmp_path / "bin"
    _fake_tool(fake_bin, "sudo", "", 1)
    for name, (output, status) in tools.items():
        _fake_tool(fake_bin, name, output, status)
    loginctl = fake_bin / "loginctl"
    loginctl.write_text(f"#!/bin/sh\necho {linger}\n")
    loginctl.chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run([str(VERIFY_SCRIPT), "--inside"],
                            capture_output=True, text=True, env=env,
                            timeout=120, check=False)
    return result.stdout.splitlines()


def test_verify_script_fails_when_the_agent_may_schedule_jobs(tmp_path) -> None:
    lines = _run_inside_with(tmp_path, {"crontab": ("no crontab for x", 1),
                                        "at": ("", 0)}, linger="yes")
    assert "FAIL agent user can use crontab (crontab -l: no crontab for x)" in lines
    assert "FAIL agent user can use at (at -l succeeded)" in lines
    assert any(line.startswith("FAIL lingering is enabled for") for line in lines)
    assert lines[-1].startswith("RESULT: FAIL")


def test_verify_script_passes_denied_cron_and_at(tmp_path) -> None:
    lines = _run_inside_with(tmp_path, {"crontab": (_CRON_DENIED, 1),
                                        "at": (_AT_DENIED, 1)})
    assert "PASS agent user cannot use crontab" in lines
    assert "PASS agent user cannot use at" in lines


# --- R3136-08: TheForge copies with an excluded table below the project roots -----------


def _sqlite(path: Path, *tables: str) -> Path:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    for table in tables:
        connection.execute(f"CREATE TABLE {table} (x)")
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def project_roots(tmp_path: Path) -> dict[str, Path]:
    projects = tmp_path / "projects"
    copy = _sqlite(projects / "shop" / "data" / "theforge.db", "tasks", "API_KEYS")
    _sqlite(projects / "shop" / "fixtures.sqlite3", "orders")
    _sqlite(projects / "shop" / "node_modules" / "x" / "keys.db", "api_keys")
    (projects / "shop" / "notes.db").write_text("not a database")
    (projects / "shop" / "link.db").symlink_to(copy)
    return {"projects": projects, "copy": copy}


def test_credential_databases_below_project_roots_are_found(
        project_roots: dict[str, Path]) -> None:
    found, truncated = isolation.find_credential_databases(
        [str(project_roots["projects"])], ["api_keys"])
    assert found == [str(project_roots["copy"])] and not truncated


def test_probe_command_probes_credential_copies_as_the_agent(
        tmp_path: Path, monkeypatch, project_roots: dict[str, Path]) -> None:
    forge = tmp_path / "TheForge"
    forge.mkdir()
    (forge / "theforge.db").write_text("x")
    monkeypatch.setattr(isolation, "THEFORGE_DB", forge / "theforge.db")
    monkeypatch.setattr(isolation, "MCP_CONFIG", tmp_path / "no-mcp.json")
    settings = _settings(tmp_path,
                         secret_scan_roots=[str(project_roots["projects"])])
    command = isolation.build_probe_command("probe", settings, [], str(tmp_path))
    copies = [command[i + 1] for i, arg in enumerate(command) if arg == "--db-copy"]
    assert str(project_roots["copy"]) in copies


def test_outer_checks_fail_on_a_world_readable_credential_copy(
        tmp_path: Path, monkeypatch, project_roots: dict[str, Path]) -> None:
    forge = tmp_path / "TheForge"
    forge.mkdir(mode=0o700)
    monkeypatch.setattr(isolation, "THEFORGE_DB", forge / "theforge.db")
    monkeypatch.setattr(isolation, "MCP_CONFIG", tmp_path / "no-mcp.json")
    monkeypatch.setattr(isolation, "_exit_status", lambda argv: 0)
    settings = _settings(tmp_path,
                         secret_scan_roots=[str(project_roots["projects"])])
    project_roots["copy"].chmod(0o644)
    failures = "\n".join(isolation._outer_checks(settings))
    assert f"{project_roots['copy']} holds an excluded table" in failures
    project_roots["copy"].chmod(0o600)
    assert isolation._outer_checks(settings) == []
