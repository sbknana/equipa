"""Task #3189: more host settings a per-run ``--dispatch-config`` keeps.

IR87-01: the MCP trust lists (``mcp_trusted_executables``,
``mcp_uvx_trusted_urls``) were read from the per-run file only. A uvx outside
the system directories (uv installs it in ``~/.local/bin``) is trusted only
when listed, so with the list in the host config every dispatch under a
per-run config was refused. The host's entries now stay; a per-run file may
only add entries.

IR87-04: a ``path_translations`` prefix a per-run file adds still maps
paths, but its ``to`` no longer widens the scaffold's mkdir allowlist.

IR87-05: a per-run file cannot turn off the security review the host config
has on.

IR87-07: an empty (or non-object) per-run ``agent_isolation`` section no
longer replaces the host's section.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import equipa.config as config_mod
from equipa import agent_runner, scaffold
from equipa.config import (
    AGENT_ISOLATION_KEY,
    PATH_TRANSLATIONS_KEY,
    PER_RUN_TRANSLATIONS_KEY,
    is_feature_enabled,
    is_security_review_enabled,
    load_dispatch_config,
    scaffold_root_targets,
    set_active_dispatch_config,
    translate_local_path,
)

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


# --- IR87-05: the security review ---------------------------------------------


@pytest.mark.parametrize("per_run_data", [
    {"security_review": False},
    {"features": {"security_review": False}},
    {"security_review": False, "features": {"security_review": "false"}},
], ids=["top-level", "feature-flag", "both"])
def test_a_per_run_config_cannot_turn_the_host_review_off(
    host: SimpleNamespace, tmp_path: Path, per_run_data: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_json(host.config, {"max_concurrent": 4})
    per_run = _write_json(tmp_path / "no-review.json", per_run_data)

    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(per_run)

    assert is_security_review_enabled(None, config) is True
    assert is_security_review_enabled(SimpleNamespace(dispatch_config=config)) is True
    assert "cannot turn the security review off" in caplog.text


def test_turning_the_review_back_on_keeps_the_other_features(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {"security_review": True})
    per_run = _write_json(tmp_path / "no-review.json", {
        "features": {"security_review": False, "hooks": True}})

    config = load_dispatch_config(per_run)

    assert is_feature_enabled(config, "hooks") is True
    assert is_feature_enabled(config, "security_review") is True


def test_a_host_with_the_review_off_leaves_it_to_the_per_run_config(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {"security_review": False})
    off = _write_json(tmp_path / "off.json", {"security_review": False})
    plain = _write_json(tmp_path / "plain.json", {"max_concurrent": 2})

    assert is_security_review_enabled(None, load_dispatch_config(off)) is False
    assert is_security_review_enabled(None, load_dispatch_config(plain)) is True


def test_the_cli_flag_still_decides_its_own_run(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    """``--no-security-review`` is the operator's explicit choice for one
    run; only the per-run file lost the power to switch it off."""
    _write_json(host.config, {"max_concurrent": 4})
    per_run = _write_json(tmp_path / "run.json", {"security_review": False})
    config = load_dispatch_config(per_run)

    assert is_security_review_enabled(
        SimpleNamespace(security_review=False), config) is False


def test_a_corrupt_host_config_keeps_the_review_on(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    host.config.write_text("{not json", encoding="utf-8")
    per_run = _write_json(tmp_path / "off.json", {"security_review": False})

    assert is_security_review_enabled(None, load_dispatch_config(per_run)) is True


# --- IR87-07: the agent_isolation section -------------------------------------

HOST_ISOLATION = {"agent_user": "equipa-agent", "exchange_dir": "/var/lib/x"}


def _host_has_isolation_on(host: SimpleNamespace) -> None:
    _write_json(host.config, {"features": {AGENT_ISOLATION_KEY: True},
                              AGENT_ISOLATION_KEY: HOST_ISOLATION})


@pytest.mark.parametrize("section", [{}, None, [], "", "equipa-agent"])
def test_an_empty_per_run_isolation_section_keeps_the_host_section(
    host: SimpleNamespace, tmp_path: Path, section: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _host_has_isolation_on(host)
    per_run = _write_json(tmp_path / "run.json", {AGENT_ISOLATION_KEY: section})

    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(per_run)

    assert config[AGENT_ISOLATION_KEY] == HOST_ISOLATION
    assert is_feature_enabled(config, AGENT_ISOLATION_KEY) is True
    assert "holds no settings" in caplog.text


def test_a_per_run_isolation_section_with_settings_is_its_own(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    """Unchanged F1 design: a per-run file with its own settings uses them."""
    _host_has_isolation_on(host)
    own = {"agent_user": "other-agent", "exchange_dir": "/var/lib/y"}
    per_run = _write_json(tmp_path / "run.json", {AGENT_ISOLATION_KEY: own})

    assert load_dispatch_config(per_run)[AGENT_ISOLATION_KEY] == own


def test_a_per_run_file_without_a_section_gets_the_host_section(
    host: SimpleNamespace, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    _host_has_isolation_on(host)
    per_run = _write_json(tmp_path / "run.json", {"max_concurrent": 1})

    with caplog.at_level(logging.WARNING, logger="equipa.config"):
        config = load_dispatch_config(per_run)

    assert config[AGENT_ISOLATION_KEY] == HOST_ISOLATION
    assert "holds no settings" not in caplog.text


def test_a_host_with_isolation_off_carries_no_section(
    host: SimpleNamespace, tmp_path: Path,
) -> None:
    _write_json(host.config, {AGENT_ISOLATION_KEY: HOST_ISOLATION})
    per_run = _write_json(tmp_path / "run.json", {AGENT_ISOLATION_KEY: {}})

    assert load_dispatch_config(per_run)[AGENT_ISOLATION_KEY] == {}


# --- IR87-04: the scaffold's mkdir allowlist ----------------------------------

SHARE = "X:\\share"


@pytest.fixture
def mount(host: SimpleNamespace, tmp_path: Path,
          monkeypatch: pytest.MonkeyPatch) -> Path:
    """A share mount the host config maps ``X:\\share`` to; the scaffold
    roots come from the config, not the environment."""
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    share_mount = tmp_path / "share"
    share_mount.mkdir()
    _write_json(host.config, {PATH_TRANSLATIONS_KEY: [
        {"from": SHARE, "to": str(share_mount)}]})
    return share_mount


def test_a_per_run_prefix_maps_paths_but_adds_no_scaffold_root(
    mount: Path, tmp_path: Path,
) -> None:
    """The reviewer's probe: a per-run ``{"from": "Q:", "to": "/"}`` made
    every absolute path a scaffold clone target."""
    per_run = _write_json(tmp_path / "wide.json", {PATH_TRANSLATIONS_KEY: [
        {"from": "Q:", "to": "/"}]})

    _run_under(per_run)

    assert translate_local_path("Q:\\etc\\x") == "/etc/x"
    assert scaffold._allowed_roots() == (mount.resolve(),)
    with pytest.raises(scaffold.ScaffoldCloneError, match="allowlisted"):
        scaffold.assert_contained_path("/etc/equipa-3189-probe")
    assert scaffold.assert_contained_path(str(mount / "Proj")) == (
        mount / "Proj").resolve()


def test_a_per_run_parent_prefix_adds_no_scaffold_root(
    mount: Path, tmp_path: Path,
) -> None:
    evil = tmp_path / "evil"
    per_run = _write_json(tmp_path / "parent.json", {PATH_TRANSLATIONS_KEY: [
        {"from": "X:", "to": str(evil)}]})

    _run_under(per_run)

    assert translate_local_path("X:\\other\\P") == f"{evil}/other/P"
    assert scaffold_root_targets() == [str(mount)]
    with pytest.raises(scaffold.ScaffoldCloneError):
        scaffold.assert_contained_path(str(evil / "other" / "P"))


def test_a_per_run_file_cannot_clear_the_record_of_its_own_entries(
    mount: Path, tmp_path: Path,
) -> None:
    per_run = _write_json(tmp_path / "sneaky.json", {
        PATH_TRANSLATIONS_KEY: [{"from": "Q:", "to": "/"}],
        PER_RUN_TRANSLATIONS_KEY: []})

    config = _run_under(per_run)

    assert config[PER_RUN_TRANSLATIONS_KEY] == [{"from": "Q:", "to": "/"}]
    assert scaffold_root_targets() == [str(mount)]


def test_a_per_run_prefix_adds_no_root_when_the_host_maps_nothing(
    host: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    _write_json(host.config, {"max_concurrent": 4})
    per_run = _write_json(tmp_path / "wide.json", {PATH_TRANSLATIONS_KEY: [
        {"from": "Q:", "to": "/"}]})

    _run_under(per_run)

    assert translate_local_path("Q:\\srv\\x") == "/srv/x"
    assert scaffold._allowed_roots() == ()


def test_a_config_registered_directly_keeps_every_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged: without a per-run file (the host config itself, or a
    config the caller registers) every target is a root."""
    monkeypatch.delenv("EQUIPA_SCAFFOLD_ALLOWED_ROOTS", raising=False)
    set_active_dispatch_config({PATH_TRANSLATIONS_KEY: [
        {"from": SHARE, "to": str(tmp_path / "a")},
        {"from": "Y:\\other", "to": str(tmp_path / "b")}]})
    try:
        assert sorted(scaffold_root_targets()) == [
            str(tmp_path / "a"), str(tmp_path / "b")]
    finally:
        set_active_dispatch_config(None)


def test_a_record_that_is_not_a_list_trusts_no_root() -> None:
    config = {PATH_TRANSLATIONS_KEY: [{"from": SHARE, "to": "/srv/share"}],
              PER_RUN_TRANSLATIONS_KEY: "everything"}

    assert scaffold_root_targets(config) == []


def test_loading_the_host_file_itself_records_nothing(mount: Path,
                                                      host: SimpleNamespace) -> None:
    config = load_dispatch_config(host.config)

    assert PER_RUN_TRANSLATIONS_KEY not in config
    assert scaffold_root_targets(config) == [str(mount)]
