"""Plugin discovery for EQUIPA extensions.

EQUIPA supports plugins via Python entry points in the ``equipa.plugins``
group.  Any installed package that declares an entry point in this group
will be loaded at startup and given access to the hooks registry.

Example plugin registration (in the plugin's pyproject.toml):

    [project.entry-points."equipa.plugins"]
    my_plugin = "my_package:register_plugin"

The entry point must be a callable that accepts a hooks registry object:

    def register_plugin(hooks):
        def on_pre_dispatch(**kwargs):
            return {"extra_context": "advisory text"}
        hooks.register("pre_dispatch", on_pre_dispatch)

Copyright 2026 Forgeborn
"""

from __future__ import annotations

import importlib.metadata
import logging

log = logging.getLogger(__name__)

PLUGIN_GROUP = "equipa.plugins"


def load_plugins(hooks) -> int:
    """Discover and load all installed EQUIPA plugins via entry points.

    Args:
        hooks: The hooks registry object (from equipa.hooks).

    Returns:
        Number of plugins successfully loaded.
    """
    loaded = 0
    try:
        eps = importlib.metadata.entry_points(group=PLUGIN_GROUP)
    except TypeError:
        # Python 3.9 fallback — entry_points() returns a dict
        eps = importlib.metadata.entry_points().get(PLUGIN_GROUP, [])

    for ep in eps:
        try:
            register_fn = ep.load()
            register_fn(hooks)
            log.info("Plugin loaded: %s", ep.name)
            print(f"  [Plugin] Loaded: {ep.name}")
            loaded += 1
        except Exception as e:
            log.warning("Plugin %s failed to load: %s", ep.name, e)
            print(f"  [Plugin] Failed to load {ep.name}: {e}")

    return loaded
