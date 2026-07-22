"""Render a :class:`ClaudeProfile` to its ``CLAUDE_CONFIG_DIR`` on disk.

The output layout is what Claude Code expects::

    <data_dir>/claude_profiles/<slug>/
        settings.json        # model, availableModels, permissions, etc.
        agents/
            <slug>.md        # one file per Subagent (YAML frontmatter + body)

Writes are **idempotent** — concurrent sessions of the same profile share
this directory by design (the settings file is profile-level state, not
session-level). Per-session state lives under
``CLAUDE_CONFIG_DIR/projects/<cwd-hash>/`` which Claude Code creates
itself based on the session's cwd.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.profiles.models import ClaudeProfile


# ---- agents ----------------------------------------------------------------


def _agent_markdown(slug: str, name: str, description: str, tools, model, prompt: str) -> str:
    """Render a single ``agents/<slug>.md`` file's contents.

    Frontmatter is YAML — Claude Code documents the keys as ``name``,
    ``description``, ``tools`` (comma-separated, or omitted), ``model``.
    We emit a minimal valid frontmatter even when the optional fields
    are ``None`` (Claude Code treats their absence as "inherit
    defaults").
    """
    lines = ["---"]
    lines.append(f"name: {_yaml_scalar(name or slug)}")
    lines.append(f"description: {_yaml_scalar(description)}")
    if tools:
        # YAML inline-list form: ``tools: [foo, bar]`` is unambiguous
        # and avoids the "is this a comma-separated string or a list"
        # ambiguity YAML allows. Tools are simple identifiers in
        # Claude Code (Read, Edit, Bash, …) so no quoting is needed;
        # _yaml_scalar would still produce a valid result, but plain
        # identifiers read more naturally in the file.
        rendered = ", ".join(str(t).strip() for t in tools if str(t).strip())
        lines.append(f"tools: [{rendered}]")
    if model:
        lines.append(f"model: {_yaml_scalar(model)}")
    lines.append("---")
    lines.append("")
    lines.append(prompt.rstrip())
    lines.append("")  # trailing newline
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """Quote a YAML scalar so names/descriptions with ``:`` or ``#`` parse.

    Claude Code's parser is permissive, but double-quoting every free-form
    string is the safest cross-parser default — no need to detect edge
    cases like a description starting with ``[`` or ``{``.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---- settings.json ---------------------------------------------------------


def _build_settings(profile: ClaudeProfile) -> dict:
    """Compose the ``settings.json`` body for this profile.

    Order of precedence (later wins on collision):
      1. ``profile.claude_settings`` (free-form keys the user added)
      2. ``availableModels`` derived from ``model_endpoints`` keys
      3. ``model`` derived from ``default_model`` if set

    We deliberately let ``claude_settings`` set ``model`` /
    ``availableModels`` first and then overwrite — so the profile's
    canonical "this profile uses these models" state always wins.
    The editor UI discourages setting these in the raw settings JSON.
    """
    out: dict = dict(profile.claude_settings)
    if profile.model_endpoints:
        out["availableModels"] = sorted(profile.model_endpoints.keys())
    if profile.default_model:
        out["model"] = profile.default_model
    return out


# ---- public API ------------------------------------------------------------


def write_profile_config(profile: ClaudeProfile, base_dir: Path) -> Path:
    """Render the profile's config dir under ``base_dir``.

    Returns the resolved path to the ``CLAUDE_CONFIG_DIR`` (i.e.
    ``<base_dir>/<slug>``). Idempotent — re-writing the same profile
    overwrites its files in place; it never deletes subdirectories
    owned by subagents that were removed from the profile (we only
    overwrite files whose slug matches a current subagent).

    Caller is responsible for passing a base directory the current
    user owns; this function does no permission juggling.
    """
    config_dir = base_dir / profile.slug
    config_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = config_dir / "agents"
    agents_dir.mkdir(exist_ok=True)

    settings = _build_settings(profile)
    (config_dir / "settings.json").write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Subagents: write one .md per entry, slugified.
    wanted_slugs: set[str] = set()
    for sa in profile.subagents:
        from src.profiles.models import slugify as _slug

        slug = _slug(sa.name) or _slug(profile.slug + "-" + sa.name)
        wanted_slugs.add(slug)
        body = _agent_markdown(
            slug=slug,
            name=sa.name,
            description=sa.description,
            tools=sa.tools,
            model=sa.model,
            prompt=sa.system_prompt,
        )
        (agents_dir / f"{slug}.md").write_text(body, encoding="utf-8")

    # Best-effort prune: remove ``agents/<slug>.md`` files that are no
    # longer in the profile. We don't fail if some couldn't be removed
    # (read-only fs, etc.) — the worst case is a stale agent that
    # Claude Code will ignore.
    if agents_dir.is_dir():
        for existing in agents_dir.glob("*.md"):
            if existing.stem not in wanted_slugs:
                try:
                    existing.unlink()
                except OSError:
                    pass

    return config_dir