"""Shared id-format helpers for Claude Code's gateway auto-discovery.

The contract
-----------
Claude Code's ``/model`` picker reads ``id`` and ``display_name``
from each entry in ``/v1/models``, AND it reads the ``availableModels``
list from ``settings.json`` on disk. Both surfaces must use the
SAME id format — otherwise Claude Code applies its filter
(``id`` contains ``claude`` or ``anthropic``, case-insensitive)
inconsistently:

  - ``/v1/models`` returns ``id = "minimax"`` → picker drops it
  - ``settings.json.availableModels = ["minimax"]`` → picker
    surfaces it but the proxy can't route ``model=minimax`` because
    the proxy never had a chance to advertise it as ``minimax-non-anthropic``

The fix: non-Claude / non-Anthropic aliases get a synthetic id
``<alias>-non-anthropic`` that passes the picker filter; the bare
alias is the human-readable label (the ``display_name`` in
``/v1/models``, the value the user types in ``/model minimax``).

This module is the single source of truth for that transformation.
Both ``src/profiles/config_dir.py`` (settings.json writer) and
``src/ui/main_window/_proxy_lifecycle.py`` (alias-rewrite augmentation)
call into here so the two surfaces can never drift.
"""

from __future__ import annotations


# Suffix appended to non-Claude aliases to give them an ``id`` that
# passes Claude Code's picker filter. Public so callers can detect
# synthetic ids (e.g. ``alias.endswith(SYNTHETIC_ALIAS_SUFFIX)``)
# without re-deriving the constant from a magic string.
SYNTHETIC_ALIAS_SUFFIX = "-non-anthropic"


def passes_claude_picker_filter(name: str) -> bool:
    """True when ``name`` would survive Claude Code's picker filter.

    Mirrors the filter documented at
    https://code.claude.com/docs/en/llm-gateway-protocol:

        Claude Code keeps an entry when its ``id`` contains
        ``claude`` or ``anthropic`` anywhere in the string, matched
        case-insensitively.

    Anything that fails this filter is silently dropped from the
    picker's auto-discovered list. We replicate the same case-fold
    here so the augmentation only adds synthetic ids for the
    aliases that actually need them.
    """
    lowered = name.lower()
    return "claude" in lowered or "anthropic" in lowered


def picker_id_for_alias(alias: str) -> str:
    """Return the ``id`` Claude Code's picker should see for this alias.

    Claude / anthropic aliases keep their bare name — the id already
    passes the picker filter. Non-Claude aliases get the synthetic
    suffix appended so the id passes the filter; the bare alias is
    still the value Claude Code uses as ``display_name`` (and the
    value the user types in ``/model minimax``).

    Examples
    --------
    >>> picker_id_for_alias("claude-opus-4-8")
    'claude-opus-4-8'
    >>> picker_id_for_alias("anthropic-claude")
    'anthropic-claude'
    >>> picker_id_for_alias("minimax")
    'minimax-non-anthropic'
    >>> picker_id_for_alias("mm")
    'mm-non-anthropic'

    The proxy's alias-rewrite layer handles BOTH the bare name
    ``minimax`` AND the synthetic ``minimax-non-anthropic``
    transparently — both rewrite to the same real model name.
    Users can pick either from the picker; either works.
    """
    if passes_claude_picker_filter(alias):
        return alias
    return f"{alias}{SYNTHETIC_ALIAS_SUFFIX}"