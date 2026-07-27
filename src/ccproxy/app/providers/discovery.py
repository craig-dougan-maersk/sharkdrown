"""Provider plugin discovery.

Discovers Provider subclasses from two sources:

1. Drop-in files in a ``providers/`` directory (configurable path).
   Any ``.py`` file is imported; all public classes that subclass
   ``Provider`` are collected.

2. Python entry points registered under the group
   ``"ccproxy.providers"``. Third-party packages can register via
   ``[project.entry-points."ccproxy.providers"]`` in pyproject.toml.

Each discovered class is instantiated with no arguments (relying on
defaults or env-var configuration) and keyed by its ``name`` attribute.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ccproxy.app.providers.base import Provider

logger = logging.getLogger("claude_code_proxy.providers.discovery")

_ENTRY_POINT_GROUP = "ccproxy.providers"


def discover_from_directory(directory: Path | str) -> dict[str, type[Provider]]:
    """Import all .py files in *directory* and collect Provider subclasses."""
    directory = Path(directory)
    found: dict[str, type[Provider]] = {}

    if not directory.is_dir():
        return found

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"ccproxy_plugin_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            logger.warning("failed to load provider plugin %s", path, exc_info=True)
            continue

        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Provider)
                and obj is not Provider
                and hasattr(obj, "name")
            ):
                found[obj.name] = obj
                logger.info("discovered provider plugin: %s from %s", obj.name, path.name)

    return found


def discover_from_entry_points() -> dict[str, type[Provider]]:
    """Load Provider subclasses registered as entry points."""
    found: dict[str, type[Provider]] = {}

    eps = entry_points()
    group = eps.get(_ENTRY_POINT_GROUP, []) if hasattr(eps, "get") else eps.select(group=_ENTRY_POINT_GROUP)

    for ep in group:
        try:
            cls = ep.load()
        except Exception:
            logger.warning("failed to load entry point %s", ep.name, exc_info=True)
            continue
        if isinstance(cls, type) and issubclass(cls, Provider) and cls is not Provider:
            found[getattr(cls, "name", ep.name)] = cls
            logger.info("discovered provider entry point: %s", ep.name)

    return found


def discover_plugins(
    directory: Path | str | None = None,
    **init_kwargs: Any,
) -> dict[str, Provider]:
    """Discover and instantiate provider plugins from all sources.

    Returns a dict of provider name -> instantiated Provider, ready to
    merge into the main providers dict.
    """
    classes: dict[str, type[Provider]] = {}

    if directory is not None:
        classes.update(discover_from_directory(directory))

    classes.update(discover_from_entry_points())

    instances: dict[str, Provider] = {}
    for name, cls in classes.items():
        try:
            instances[name] = cls(**init_kwargs) if init_kwargs else cls()
        except Exception:
            logger.warning("failed to instantiate provider plugin %s", name, exc_info=True)

    return instances
