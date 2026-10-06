"""Task #3189: more host settings a per-run ``--dispatch-config`` keeps.

IR87-01: the MCP trust lists (``mcp_trusted_executables``,
``mcp_uvx_trusted_urls``) were read from the per-run file only. A uvx outside
the system directories (uv installs it in ``~/.local/bin``) is trusted only
when listed, so with the list in the host config every dispatch under a
per-run config was refused. The host's entries now stay; a per-run file may
only add entries.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.config as config_mod
from equipa import agent_runner
from equipa.config import load_dispatch_config, set_active_dispatch_config

TRUSTED = agent_runner.MCP_TRUSTED_EXECUTABLES_KEY
TRUSTED_URLS = agent_runner.MCP_UVX_TRUSTED_URLS_KEY
INDEX_URL = "https://wheels.example.test/simple"


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The host dispatch config (absent until a test writes it), a uvx in a
    user's ``~/.local/bin`` and a project directory."""
    uvx = tmp_path / "home" / "operator" / ".local" / "bin" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    uvx.chmod(0o755)
    state = SimpleNamespace(config=tmp_path / "host" / "dispatch_config.json",
                            uvx=uvx, project=tmp_path / "project")
    state.config.parent.mkdir()
    state.project.mkdir()
    monkeypatch.setattr(config_mod, "host_dispatch_config_path",
                        lambda: state.config)
    monkeypatch.setattr(agent_runner, "PROJECT_DIRS", {})
    monkeypatch.chdir(tmp_path)
    set_active_dispatch_config({})
    yield state
    set_active_dispatch_config(None)


def _write_json(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _check_uvx_server(host: SimpleNamespace, tmp_path: Path,
                      *uvx_options: str) -> None:
    """agent_runner's launch check on prod's theforge server shape, run by
    the uvx in ``~/.local/bin``."""
    mcp_config = _write_json(tmp_path / "mcp_config.json", {"mcpServers": {
        "theforge": {"command": str(host.uvx),
                     "args": [*uvx_options, "mcp-server-sqlite",
                              "--db-path", "/abs/theforge.db"]}}})
    agent_runner._check_mcp_servers(mcp_config, (host.project,))


def _run_under(per_run: Path) -> dict:
    """Load and register ``per_run`` the way the CLI does."""
    config = load_dispatch_config(per_run)
    set_active_dispatch_config(config)
    return config


# --- IR87-01: the MCP trust lists ---------------------------------------------


def test_a_per_run_config_keeps_the_host_trusted_uvx(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    """The reviewer's case 4: the host lists ``~/.local/bin/uvx``, the
    operator's per-run file only picks projects."""
    _write_json(host.config, {TRUSTED: [str(host.uvx)]})
    per_run = _write_json(tmp_path / "run.json",
                          {"only_projects": [23], "max_concurrent": 2})

    config = _run_under(per_run)

    assert config[TRUSTED] == [str(host.uvx)]
    _check_uvx_server(host, tmp_path)


def test_without_a_trust_entry_the_uvx_is_refused(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    """Control: the launch check above accepts the uvx only because of the
    carried entry."""
    _write_json(host.config, {"max_concurrent": 4})
    per_run = _write_json(tmp_path / "run.json", {"only_projects": [23]})

    _run_under(per_run)

    with pytest.raises(agent_runner.AgentDispatchRefused, match=TRUSTED):
        _check_uvx_server(host, tmp_path)


def test_a_per_run_config_keeps_the_host_trusted_index_url(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)],
                              TRUSTED_URLS: [INDEX_URL]})
    per_run = _write_json(tmp_path / "run.json", {"only_projects": [23]})

    _run_under(per_run)

    _check_uvx_server(host, tmp_path, f"--index-url={INDEX_URL}")


def test_an_index_url_the_host_does_not_list_is_still_refused(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)],
                              TRUSTED_URLS: [INDEX_URL]})
    per_run = _write_json(tmp_path / "run.json", {"only_projects": [23]})

    _run_under(per_run)

    with pytest.raises(agent_runner.AgentDispatchRefused, match=TRUSTED_URLS):
        _check_uvx_server(host, tmp_path,
                          "--index-url=https://other.example.test/simple")


@pytest.mark.parametrize("per_run_value", [[], None, "/usr/bin/uvx",
                                           {"path": "/usr/bin/uvx"}])
def test_a_per_run_config_cannot_drop_the_host_entries(
    host: SimpleNamespace, tmp_path: Path, per_run_value: object,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)],
                              TRUSTED_URLS: [INDEX_URL]})
    per_run = _write_json(tmp_path / "run.json", {TRUSTED: per_run_value,
                                                  TRUSTED_URLS: per_run_value})

    config = _run_under(per_run)

    assert config[TRUSTED] == [str(host.uvx)]
    assert config[TRUSTED_URLS] == [INDEX_URL]
    _check_uvx_server(host, tmp_path, f"--index-url={INDEX_URL}")


def test_a_per_run_config_may_add_entries(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    other_url = "https://mirror.example.test/simple"
    _write_json(host.config, {TRUSTED: ["/opt/tools/bin/uvx"],
                              TRUSTED_URLS: [INDEX_URL]})
    per_run = _write_json(tmp_path / "run.json", {
        TRUSTED: [str(host.uvx), "/opt/tools/bin/uvx"],
        TRUSTED_URLS: [other_url]})

    config = _run_under(per_run)

    assert config[TRUSTED] == ["/opt/tools/bin/uvx", str(host.uvx)]
    assert config[TRUSTED_URLS] == [INDEX_URL, other_url]
    _check_uvx_server(host, tmp_path, f"--index-url={other_url}")


def test_a_per_run_value_that_is_not_a_list_is_logged(
    host: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)]})
    per_run = _write_json(tmp_path / "run.json", {TRUSTED: str(host.uvx)})

    with caplog.at_level(logging.ERROR, logger="equipa.config"):
        load_dispatch_config(per_run)

    assert f"{TRUSTED!r} must be a list" in caplog.text


def test_the_trust_lists_are_carried_when_the_per_run_file_is_missing(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)]})

    _run_under(tmp_path / "mistyped.json")

    _check_uvx_server(host, tmp_path)


def test_a_carried_trust_list_is_a_copy(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {TRUSTED: [str(host.uvx)]})
    per_run = _write_json(tmp_path / "run.json", {"max_concurrent": 1})

    first = load_dispatch_config(per_run)
    first[TRUSTED].append("/tmp/evil/uvx")

    assert load_dispatch_config(per_run)[TRUSTED] == [str(host.uvx)]


def test_the_carried_keys_are_the_ones_agent_runner_reads() -> None:
    """config.py repeats the names (agent_runner imports config)."""
    assert set(config_mod.HOST_TRUST_LIST_KEYS) == {TRUSTED, TRUSTED_URLS}
