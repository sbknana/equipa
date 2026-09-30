#!/usr/bin/env python3
"""Task 3127 / review finding P2A-06: preflight runs project code scrubbed.

``preflight_build_check`` and ``auto_install_dependencies`` execute project
code in the orchestrator (package.json build/postinstall scripts, setup.py,
MSBuild targets), including a build script the auto-fix agent wrote moments
earlier. They ran with the orchestrator's complete environment. They now get
the allowlisted agent environment.

The project's scripts here dump their environment to a file. A fake ``npm``
first on PATH runs package.json scripts through /bin/sh the way npm does.
All credential values are fake sentinels.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

import equipa.config as equipa_config
from equipa import preflight

SENTINELS = {
    "DATABASE_URL": "postgresql://fake:FAKE-3127-preflight-pw@db.invalid/x",
    "PGPASSWORD": "FAKE-3127-preflight-pgpassword",
    "GITHUB_TOKEN": "FAKE-3127-preflight-github-token",
    "ANTHROPIC_API_KEY": "sk-FAKE-3127-preflight-api-key",
    "EQUIPA_FAKE_DOTENV_SECRET": "FAKE-3127-preflight-dotenv-secret",
}

# Runs `npm run <script>` / `npm install` (+ postinstall) from package.json.
FAKE_NPM = f'''#!{sys.executable}
import json, subprocess, sys
scripts = json.load(open("package.json", encoding="utf-8")).get("scripts", {{}})
if sys.argv[1:2] == ["run"]:
    sys.exit(subprocess.run(scripts[sys.argv[2]], shell=True).returncode)
if sys.argv[1:2] == ["install"]:
    if "postinstall" in scripts:
        sys.exit(subprocess.run(scripts["postinstall"], shell=True).returncode)
    sys.exit(0)
sys.exit(2)
'''

DUMP_ENV = '''import json, os, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(dict(os.environ), fh)
'''


@pytest.fixture(autouse=True)
def _isolated_dispatch_config(monkeypatch):
    monkeypatch.setattr(equipa_config, "_active_dispatch_config", {})


@pytest.fixture
def node_project(tmp_path, monkeypatch):
    """A node project whose scripts dump their env, and a fake npm on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(FAKE_NPM, encoding="utf-8")
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)

    project = tmp_path / "project"
    project.mkdir()
    (project / "dump_env.py").write_text(DUMP_ENV, encoding="utf-8")
    return project


def _write_package_json(project: Path, **scripts: str) -> None:
    (project / "package.json").write_text(
        json.dumps({"name": "fake", "scripts": scripts}), encoding="utf-8")


def _assert_scrubbed(dump: Path) -> None:
    assert dump.exists(), "the project script did not run"
    seen = json.loads(dump.read_text(encoding="utf-8"))
    leaked = {name for name in SENTINELS if name in seen}
    assert not leaked, f"credentials reached project code: {sorted(leaked)}"
    for value in SENTINELS.values():
        assert value not in json.dumps(seen)
    assert seen.get("PATH")  # still a working environment


def test_build_check_runs_the_build_script_with_the_agent_env(node_project):
    dump = node_project / "build_env.json"
    _write_package_json(node_project,
                        build=f"{sys.executable} dump_env.py {dump}")

    ok, language, _ = asyncio.run(preflight.preflight_build_check(
        str(node_project)))

    assert (ok, language) == (True, "node")
    _assert_scrubbed(dump)


def test_auto_install_runs_postinstall_with_the_agent_env(node_project):
    dump = node_project / "postinstall_env.json"
    _write_package_json(node_project,
                        postinstall=f"{sys.executable} dump_env.py {dump}")

    asyncio.run(preflight.auto_install_dependencies(str(node_project)))

    _assert_scrubbed(dump)


def test_recheck_after_the_autofix_agent_runs_with_the_agent_env(
        node_project, monkeypatch):
    """The auto-fix agent writes the build script preflight runs next."""
    dump = node_project / "recheck_env.json"
    _write_package_json(node_project, build="exit 1")

    async def fake_autofix_agent(role, task_dict, project_dir, *args, **kwargs):
        _write_package_json(Path(project_dir),
                            build=f"{sys.executable} dump_env.py {dump}")
        return {"success": True, "num_turns": 1, "cost": 0.0}, 0.0

    monkeypatch.setattr(preflight, "_dispatch_autofix_agent", fake_autofix_agent)

    fixed, _cost, summary = asyncio.run(preflight._handle_preflight_failure(
        {"id": 3127}, str(node_project), {}, "node", "fake build error", None))

    assert fixed, summary
    _assert_scrubbed(dump)
