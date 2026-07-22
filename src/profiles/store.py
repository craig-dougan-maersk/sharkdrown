"""Profile storage — load/save/list Claude profiles.

The store is a single ``profiles.json`` file in the app data dir
(chmod 600). It tracks:

- A list of :class:`ClaudeProfile` records (one per profile)
- The name of the active profile (drives the proxy's routing)
- A ``schema_version`` for forward-compat migrations

Migration
---------
On first read where ``profiles.json`` is absent AND the legacy
:class:`src.proxy.proxy_config.ProxyConfig` has a non-empty
``model_endpoints``, we seed a single ``"Default"`` profile from
those endpoints. Idempotent — the seed only fires when neither file
exists, so a user who manually deletes ``profiles.json`` doesn't
get a duplicate seeding. The legacy QSettings keys stay in place;
they remain a fallback for the proxy when no profile is active.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from pathlib import Path

from src.profiles.credential_store import CredentialStore
from src.profiles.models import ClaudeProfile, Subagent, slugify


SCHEMA_VERSION = 1
DEFAULT_PROFILE_NAME = "Default"
# Profile-level sentinel used when a profile doesn't configure its own
# API key. ``build_claude_session_env`` writes this string verbatim as
# ``ANTHROPIC_API_KEY`` so Claude Code doesn't refuse to start; the
# proxy then either rejects (Phase 1-2) or injects a real key (Phase 3).
SENTINEL_API_KEY = "sk-proxy-injected"


class ProfileStore:
    """CRUD facade over ``profiles.json``.

    The store is the only writer of the file. :class:`CredentialStore`
    holds the actual API keys — the profile record carries only a
    ``ref`` string. ``save()`` accepts a full list and writes atomically
    via a ``.tmp`` + ``os.replace`` so a crash mid-write doesn't
    truncate an existing file.
    """

    def __init__(
        self,
        path: Path,
        *,
        credential_store: CredentialStore | None = None,
        legacy_proxy_config_provider=None,
    ) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._creds = credential_store or CredentialStore(
            path.parent / "credentials.json"
        )
        # Callable returning ``ProxyConfig.model_endpoints``. Provided
        # by the GUI on construction so the store doesn't need to
        # import QSettings at module load — keeps the store
        # unit-testable without a QApplication.
        self._legacy_proxy_config_provider = legacy_proxy_config_provider

    # ---- public API --------------------------------------------------------

    @property
    def credential_store(self) -> CredentialStore:
        return self._creds

    def load(self) -> list[ClaudeProfile]:
        """Read all profiles from disk.

        Performs the first-launch migration if applicable — see the
        module docstring. Returns an empty list when the file is
        missing AND no legacy data exists; otherwise seeds a
        ``"Default"`` profile from the legacy ``ProxyConfig``
        ``model_endpoints``.
        """
        data = self._read_raw()
        if data is None:
            # First launch (or someone deleted the file). Try the
            # migration; if no legacy data, start empty so the user
            # creates profiles intentionally via the Manage Profiles
            # dialog. We deliberately don't seed a "Default" when
            # there's no legacy data — silently materialising an
            # empty profile would be confusing in the menu.
            seeded = self._seed_from_legacy_or_default()
            self._save_raw(seeded)
            profiles = self._parse_profiles(seeded["profiles"])
            return self._dedup_slugs(profiles)
        profiles = self._parse_profiles(data.get("profiles", []))
        return self._dedup_slugs(profiles)

    def get_active_name(self) -> str | None:
        """Name of the currently-active profile (``None`` if unset/missing)."""
        data = self._read_raw()
        if data is None:
            return None
        active = data.get("active_profile")
        return active if isinstance(active, str) and active else None

    def get_active(self) -> ClaudeProfile | None:
        """Return the active profile or ``None`` if unset / not found."""
        profiles = self.load()
        active_name = self.get_active_name()
        if active_name is None:
            # No explicit active — fall back to first profile so the
            # menu has a sensible default.
            return profiles[0] if profiles else None
        for p in profiles:
            if p.name == active_name:
                return p
        # Active points at a deleted profile — silently retarget to
        # the first one (the most likely real-world case is a profile
        # that was removed via the editor).
        return profiles[0] if profiles else None

    def save(
        self,
        profiles: list[ClaudeProfile],
        active_name: str | None = None,
    ) -> None:
        """Persist the full profile list + active selection.

        ``active_name`` defaults to whatever's already on disk (or the
        first profile's name on first call). Caller passes ``None`` to
        mean "don't change the active selection".
        """
        # Defensive copy + dedup-by-slug so a UI bug that lets two
        # profiles collide doesn't blow up the file format.
        profiles = self._dedup_slugs(copy.deepcopy(profiles))

        # Resolve the active name before serialisation so we know what
        # to write — ``None`` or absent profile → first one's name.
        if active_name is None:
            active_name = self.get_active_name()
        if active_name is None or not any(p.name == active_name for p in profiles):
            active_name = profiles[0].name if profiles else None

        payload = {
            "schema_version": SCHEMA_VERSION,
            "active_profile": active_name,
            "profiles": [_serialize_profile(p) for p in profiles],
        }
        self._save_raw(payload)

    def set_active(self, name: str) -> None:
        """Set the active profile by name.

        Raises :class:`KeyError` if no profile with that name exists.
        Persists immediately so a subsequent ``get_active()`` reflects
        the change.
        """
        profiles = self.load()
        if not any(p.name == name for p in profiles):
            raise KeyError(f"no profile named {name!r}")
        self.save(profiles, active_name=name)

    def upsert(self, profile: ClaudeProfile) -> None:
        """Insert-or-replace a single profile by ``name``.

        Used by the profile editor's ``Save`` action. Preserves the
        active selection if it pointed at something other than this
        profile; otherwise promotes the upserted profile to active
        (so editing the "Default" profile doesn't silently deactivate
        itself).
        """
        profiles = self.load()
        active_name = self.get_active_name()
        replaced = False
        for i, p in enumerate(profiles):
            if p.name == profile.name:
                profiles[i] = profile
                replaced = True
                break
        if not replaced:
            profiles.append(profile)
        # If the active profile was the one we replaced, keep it.
        # Otherwise leave the existing active alone (a user might be
        # editing a non-active profile in the background).
        new_active = active_name if replaced else None
        self.save(profiles, active_name=new_active)

    def delete(self, name: str) -> None:
        """Remove the named profile.

        No-op if it doesn't exist. If the deleted profile was active,
        the next ``load()`` picks a new active (first remaining).
        """
        profiles = self.load()
        kept = [p for p in profiles if p.name != name]
        if len(kept) == len(profiles):
            return
        self.save(kept, active_name=None)  # let save() pick a new active

    # ---- internals ---------------------------------------------------------

    def _read_raw(self) -> dict | None:
        """Return the raw JSON payload, or ``None`` if the file is missing."""
        if not self._path.is_file():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_raw(self, data: dict) -> None:
        """Atomic JSON write with chmod 600 re-asserted on save."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def _seed_from_legacy_or_default(self) -> dict:
        """First-launch seed.

        Only materialises a ``"Default"`` profile when there's
        legacy ``ProxyConfig.model_endpoints`` to migrate. An empty
        ``profiles.json`` otherwise — the user creates profiles
        intentionally via the UI.
        """
        endpoints: dict[str, str] = {}
        if self._legacy_proxy_config_provider is not None:
            try:
                cfg = self._legacy_proxy_config_provider()
                endpoints = dict(getattr(cfg, "model_endpoints", {}) or {})
            except Exception:  # noqa: BLE001 — never let migration break startup
                endpoints = {}
        profiles: list[dict] = []
        active: str | None = None
        if endpoints:
            default = ClaudeProfile(
                name=DEFAULT_PROFILE_NAME,
                slug=slugify(DEFAULT_PROFILE_NAME),
                model_endpoints=endpoints,
            )
            profiles = [_serialize_profile(default)]
            active = default.name
        return {
            "schema_version": SCHEMA_VERSION,
            "active_profile": active,
            "profiles": profiles,
        }

    @staticmethod
    def _parse_profiles(raw_list) -> list[ClaudeProfile]:
        out: list[ClaudeProfile] = []
        if not isinstance(raw_list, list):
            return out
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            try:
                out.append(_deserialize_profile(entry))
            except Exception:  # noqa: BLE001 — skip corrupt entries, don't fail load
                continue
        return out

    @staticmethod
    def _dedup_slugs(profiles: list[ClaudeProfile]) -> list[ClaudeProfile]:
        """Ensure every slug is unique by suffixing duplicates.

        Two profiles whose ``name`` slugifies to the same thing
        (e.g. ``"My Profile"`` and ``"my_profile"``) would otherwise
        collide on the ``claude_profiles/<slug>/`` directory. We don't
        rename the human-readable ``name`` — just append ``-2``,
        ``-3`` etc. to the slug until it's unique. Slugs are also
        regenerated from the (current) name so a later rename stays
        consistent.
        """
        seen: set[str] = set()
        out: list[ClaudeProfile] = []
        for p in profiles:
            base = slugify(p.name)
            slug = base
            n = 2
            while slug in seen:
                slug = f"{base}-{n}"
                n += 1
            seen.add(slug)
            if p.slug != slug:
                p = copy.copy(p)
                p.slug = slug
            out.append(p)
        return out

    @property
    def path(self) -> Path:
        return self._path


# ---- (de)serialisation helpers --------------------------------------------


def _serialize_profile(p: ClaudeProfile) -> dict:
    d = asdict(p)
    # ``asdict`` recursively converts Subagent dataclasses too; that's
    # what we want — drop the ``None`` fields so the JSON stays tidy.
    subagents = []
    for sa in d.get("subagents", []):
        if not isinstance(sa, dict):
            continue
        sa = {k: v for k, v in sa.items() if v is not None}
        subagents.append(sa)
    d["subagents"] = subagents
    # Drop empty optional fields so the JSON doesn't carry noise.
    if d.get("default_model") is None:
        d.pop("default_model", None)
    if d.get("api_key_ref") is None:
        d.pop("api_key_ref", None)
    if d.get("session_cwd") is None:
        d.pop("session_cwd", None)
    return d


def _deserialize_profile(d: dict) -> ClaudeProfile:
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("profile missing name")
    slug = str(d.get("slug") or "").strip() or slugify(name)
    raw_endpoints = d.get("model_endpoints") or {}
    if not isinstance(raw_endpoints, dict):
        raw_endpoints = {}
    endpoints = {str(k): str(v) for k, v in raw_endpoints.items() if v}
    default_model = d.get("default_model")
    if default_model is not None:
        default_model = str(default_model) or None
    api_key_ref = d.get("api_key_ref")
    if api_key_ref is not None:
        api_key_ref = str(api_key_ref) or None
    env_vars_raw = d.get("env_vars") or {}
    if not isinstance(env_vars_raw, dict):
        env_vars_raw = {}
    env_vars = {str(k): str(v) for k, v in env_vars_raw.items()}
    settings_raw = d.get("claude_settings") or {}
    if not isinstance(settings_raw, dict):
        settings_raw = {}
    subagents_raw = d.get("subagents") or []
    if not isinstance(subagents_raw, list):
        subagents_raw = []
    subagents = []
    for sa in subagents_raw:
        if not isinstance(sa, dict):
            continue
        subagents.append(
            Subagent(
                name=str(sa.get("name") or "").strip(),
                description=str(sa.get("description") or ""),
                system_prompt=str(sa.get("system_prompt") or ""),
                tools=(sa.get("tools") if isinstance(sa.get("tools"), list) else None),
                model=(str(sa.get("model")) if sa.get("model") is not None else None),
            )
        )
    session_cwd = d.get("session_cwd")
    if session_cwd is not None:
        session_cwd = str(session_cwd) or None
    return ClaudeProfile(
        name=name,
        slug=slug,
        model_endpoints=endpoints,
        default_model=default_model,
        api_key_ref=api_key_ref,
        env_vars=env_vars,
        claude_settings=settings_raw,
        subagents=subagents,
        session_cwd=session_cwd,
    )