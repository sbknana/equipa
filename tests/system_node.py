"""A system node of the test's own for the MCP launch checks (task 3169).

The launch check (agent_runner._check_mcp_servers) applies its node rules
only to the real node: a command that is the same file as the ``node`` or
``nodejs`` found in the system command directories
(agent_runner._SYSTEM_COMMAND_DIRS, RR3138-C). The tests named
/usr/bin/node, which a clean CI runner does not have (its node, if any,
lives elsewhere), so every accepted node row failed there and every refused
one was refused for being no real node instead of for its node option.

``install_system_node`` writes executable ``node`` and ``nodejs`` files
into a directory of the test's own and lists that directory first among the
system command directories, as a system install would be. The rows then run
the same checks on every host, whether or not it has node.

Copyright 2026 Forgeborn
"""

from __future__ import annotations

from pathlib import Path

import pytest

from equipa import agent_runner

NODE_NAMES = ("node", "nodejs")


def install_system_node(monkeypatch: pytest.MonkeyPatch,
                        directory: Path) -> dict[str, str]:
    """Install ``node`` and ``nodejs`` in ``directory`` as system commands.

    Returns the absolute path of each by name. The fakes only exist to be
    named: the launch check reads a command, it never runs one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for name in NODE_NAMES:
        executable = directory / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        paths[name] = str(executable)
    monkeypatch.setattr(agent_runner, "_SYSTEM_COMMAND_DIRS",
                        (str(directory), *agent_runner._SYSTEM_COMMAND_DIRS))
    return paths
