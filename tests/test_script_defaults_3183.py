"""Task #3183 (IR80-05): the scripts' default locations sit beside the
checkout holding them (``<share>/Equipa-repo`` -> ``<share>/Athena``,
``<share>/TheForge/theforge.db``, ``<share>/Equipa-prod``), not at the
operator's own paths.

The shell scripts are never run whole here: an old default would point a
run at a real checkout of the host. Only their default assignments are
evaluated, copied into a script at the same place in a temporary checkout.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _default_assignments(script: Path, names: tuple[str, ...]) -> list[str]:
    """The top-level ``NAME="${NAME:-...}"`` lines (and helper assignments)
    of ``script`` for ``names``, in source order."""
    pattern = re.compile(rf"^(?:{'|'.join(names)})=.*$", re.MULTILINE)
    lines = pattern.findall(script.read_text(encoding="utf-8"))
    assert lines, f"no default assignment of {names} in {script.name}"
    return lines


def _evaluated_defaults(
    tmp_path: Path, script_name: str, names: tuple[str, ...], printed: tuple[str, ...],
) -> dict[str, str]:
    """Evaluate the default assignments of ``scripts/<script_name>`` from a
    copy at ``<tmp>/share/Equipa-repo/scripts/``, with none of the
    variables set, and return the values of ``printed``."""
    bash = shutil.which("bash")
    assert bash is not None, "bash is required"
    scripts = tmp_path / "share" / "Equipa-repo" / "scripts"
    scripts.mkdir(parents=True)
    probe = scripts / script_name
    body = _default_assignments(SCRIPTS / script_name, names)
    body += [f'printf "%s=%s\\n" {name} "${{{name}}}"' for name in printed]
    probe.write_text("set -euo pipefail\n" + "\n".join(body) + "\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in {*names, "EQUIPA_PROD_DIR"}}
    result = subprocess.run([bash, str(probe)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_the_truth_sync_defaults_sit_beside_its_checkout(tmp_path: Path) -> None:
    values = _evaluated_defaults(
        tmp_path, "athena_truth_sync.sh",
        ("EQUIPA_REPO", "ATHENA_DIR", "THEFORGE_DB"),
        ("EQUIPA_REPO", "ATHENA_DIR", "THEFORGE_DB"))
    share = (tmp_path / "share").resolve()

    assert values == {
        "EQUIPA_REPO": str(share / "Equipa-repo"),
        "ATHENA_DIR": str(share / "Athena"),
        "THEFORGE_DB": str(share / "TheForge" / "theforge.db"),
    }


def test_the_deploy_target_defaults_to_the_prod_checkout_beside_the_source(
    tmp_path: Path,
) -> None:
    values = _evaluated_defaults(
        tmp_path, "deploy-equipa-prod.sh", ("DEPLOY_SCRIPT_REPO", "PROD_DIR"),
        ("PROD_DIR",))

    assert values == {"PROD_DIR": str((tmp_path / "share").resolve() / "Equipa-prod")}


def test_the_deploy_target_variable_still_wins(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    lines = _default_assignments(SCRIPTS / "deploy-equipa-prod.sh",
                                 ("DEPLOY_SCRIPT_REPO", "PROD_DIR"))
    probe = tmp_path / "probe.sh"
    probe.write_text("\n".join([*lines, 'printf "%s" "$PROD_DIR"']) + "\n",
                     encoding="utf-8")
    result = subprocess.run([bash, str(probe)], capture_output=True, text=True,
                            env={**os.environ, "EQUIPA_PROD_DIR": "/srv/share/prod"},
                            check=False)

    assert (result.returncode, result.stdout) == (0, "/srv/share/prod")


def _load_script(monkeypatch: pytest.MonkeyPatch, name: str) -> ModuleType:
    """A fresh import of ``scripts/<name>.py`` (its defaults are read at
    import), registered for the test only (dataclasses look it up)."""
    module_name = f"script_{name}_3183"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_the_import_audit_searches_its_checkout_and_the_share_around_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = _load_script(monkeypatch, "audit_equipa_imports")

    assert audit.DEFAULT_ROOTS == (REPO_ROOT, REPO_ROOT.parent)


def test_the_harness_sweep_database_defaults_beside_its_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THEFORGE_DB", raising=False)
    sweep = _load_script(monkeypatch, "equipa_harness_sweep")

    assert sweep.THEFORGE_DB == str(REPO_ROOT.parent / "TheForge" / "theforge.db")


def test_the_harness_sweep_database_variable_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("THEFORGE_DB", str(tmp_path / "theforge.db"))
    sweep = _load_script(monkeypatch, "equipa_harness_sweep")

    assert sweep.THEFORGE_DB == str(tmp_path / "theforge.db")
