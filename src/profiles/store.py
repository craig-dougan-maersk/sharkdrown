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
import logging
import os
import sys
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

logger = logging.getLogger("sharkdrown.profiles")

_IS_POSIX = sys.platform != "win32"

from src.profiles.credential_store import CredentialStore
from src.profiles.models import (
    ClaudeProfile,
    ModelEndpoint,
    Provider,
    Subagent,
    slugify,
)


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
        # RLock because public methods call each other (e.g. save → get_active_name,
        # upsert → load → save). Re-entrant so nested acquisitions from the
        # same thread don't deadlock.
        self._lock: threading.RLock = threading.RLock()

    # ---- public API --------------------------------------------------------

    @property
    def credential_store(self) -> CredentialStore:
        return self._creds

    def load(self) -> list[ClaudeProfile]:
        """Read all profiles from disk.

        Performs the first-launch migration if applicable — see the
        module docstring. Always materialises a ``"Default"`` profile
        (the seed function does this whether or not legacy data
        exists). The returned list is ordered: ``Default`` first,
        then everything else in their on-disk order.
        """
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> list[ClaudeProfile]:
        data = self._read_raw()
        if data is None:
            # First launch (or someone deleted the file). Always
            # seed a "Default" profile — the user expects it to be
            # there, and it acts as the template for new profiles.
            seeded = self._seed_from_legacy_or_default()
            self._save_raw(seeded)
            profiles = self._parse_profiles(seeded["profiles"])
            return self._ensure_default_present(profiles)
        profiles = self._parse_profiles(data.get("profiles", []))
        had_default = any(p.name == DEFAULT_PROFILE_NAME for p in profiles)
        profiles = self._ensure_default_present(profiles)
        profiles = self._dedup_slugs(profiles)
        if not had_default:
            # A Default was fabricated in-memory; persist it so it survives
            # the next load() call rather than being re-fabricated each time.
            active_name = data.get("active_profile") or DEFAULT_PROFILE_NAME
            self._save_raw({
                "schema_version": data.get("schema_version", SCHEMA_VERSION),
                "active_profile": active_name,
                "profiles": [_serialize_profile(p) for p in profiles],
            })
        return profiles

    def get_active_name(self) -> str | None:
        """Name of the currently-active profile (``None`` if unset/missing)."""
        with self._lock:
            data = self._read_raw()
            if data is None:
                return None
            active = data.get("active_profile")
            return active if isinstance(active, str) and active else None

    def get_active(self) -> ClaudeProfile | None:
        """Return the active profile or ``None`` if unset / not found."""
        with self._lock:
            profiles = self._load_unlocked()
            data = self._read_raw()
            active_name = data.get("active_profile") if data else None
            active_name = active_name if isinstance(active_name, str) and active_name else None
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
        with self._lock:
            # Defensive copy + dedup-by-slug so a UI bug that lets two
            # profiles collide doesn't blow up the file format.
            profiles = self._dedup_slugs(copy.deepcopy(profiles))

            # Resolve the active name before serialisation so we know what
            # to write — ``None`` or absent profile → first one's name.
            if active_name is None:
                data = self._read_raw()
                active_name = data.get("active_profile") if data else None
                active_name = active_name if isinstance(active_name, str) and active_name else None
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
        with self._lock:
            profiles = self._load_unlocked()
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
        with self._lock:
            profiles = self._load_unlocked()
            data = self._read_raw()
            active_name = data.get("active_profile") if data else None
            active_name = active_name if isinstance(active_name, str) and active_name else None
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

        No-op if it doesn't exist. The ``Default`` profile is
        non-deletable — a UI bug or a manual ``profiles.json``
        edit shouldn't be able to remove the template profile.
        If the deleted profile was active, the next ``load()`` picks
        a new active (first remaining).
        """
        with self._lock:
            if name == DEFAULT_PROFILE_NAME:
                # Belt-and-braces: the Manage Profiles dialog disables
                # the Delete button, but guard the underlying API too.
                return
            profiles = self._load_unlocked()
            kept = [p for p in profiles if p.name != name]
            if len(kept) == len(profiles):
                return
            self.save(kept, active_name=None)  # let save() pick a new active

    def ensure_default(self) -> ClaudeProfile:
        """Return the Default profile, creating one if it doesn't exist.

        Used by the ``Manage Profiles`` "Add New" flow to guarantee
        there's always a template to seed from, even if the user
        somehow deleted the on-disk file. The returned profile is
        also persisted (as a defensive save) so a follow-up call
        to ``upsert`` of a "new profile seeded from default" works
        without a write race.
        """
        with self._lock:
            profiles = self._load_unlocked()
            for p in profiles:
                if p.name == DEFAULT_PROFILE_NAME:
                    return p
            # Fall back to a brand-new seed (no legacy data here; we've
            # already read the file once).
            default = ClaudeProfile(
                name=DEFAULT_PROFILE_NAME,
                slug=slugify(DEFAULT_PROFILE_NAME),
                is_default=True,
            )
            profiles.append(default)
            self.save(profiles, active_name=DEFAULT_PROFILE_NAME)
            return default

    # ---- internals ---------------------------------------------------------

    def _read_raw(self) -> dict | None:
        """Return the raw JSON payload, or ``None`` if the file is missing.

        When the file exists but is corrupt (JSON parse error, wrong type),
        a backup is written alongside the original before returning ``None``
        so the caller's re-seed path doesn't silently destroy recoverable data.
        """
        if not self._path.is_file():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self._backup_corrupt_file(exc)
            return None
        if not isinstance(data, dict):
            logger.error(
                "profiles.json has unexpected top-level type %s; re-seeding. "
                "Backup written to %s.corrupt.",
                type(data).__name__,
                self._path,
            )
            self._backup_corrupt_file(TypeError(f"expected dict, got {type(data).__name__}"))
            return None
        return data

    def _backup_corrupt_file(self, exc: Exception) -> None:
        """Copy the corrupt profiles.json to a .corrupt sidecar for recovery."""
        import shutil

        backup = self._path.with_suffix(".json.corrupt")
        try:
            shutil.copy2(self._path, backup)
            logger.error(
                "profiles.json is corrupt (%s); backed up to %s and re-seeding. "
                "Inspect the backup to recover your profiles.",
                exc,
                backup,
            )
        except OSError as copy_exc:
            logger.error(
                "profiles.json is corrupt (%s) and backup failed (%s); "
                "re-seeding from defaults. The corrupt file will be overwritten.",
                exc,
                copy_exc,
            )

    def _save_raw(self, data: dict) -> None:
        """Atomic JSON write with chmod 600 (POSIX only) re-asserted on save."""
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, suffix=".tmp", prefix=self._path.stem + "_"
        )
        tmp = Path(tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                if _IS_POSIX:
                    fh.flush()
                    os.fsync(fh.fileno())
                    os.fchmod(fh.fileno(), 0o600)
            os.replace(tmp, self._path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _seed_from_legacy_or_default(self) -> dict:
        """First-launch seed.

        Always materialises a ``"Default"`` profile — the user
        expects there to be a default at the top of the list that
        can't be deleted, and which acts as the template for new
        profiles. If there's legacy ``ProxyConfig`` data to migrate
        (host / port / config_dir / model_endpoints / default_endpoint)
        we copy it into the Default profile; otherwise the Default
        profile starts with the dataclass defaults.

        Legacy ``model_endpoints`` are bare ``{model: url}`` strings.
        We dedup the URLs into :class:`Provider` records keyed by a
        slug of the URL host (or the URL itself when it can't be
        parsed) and have each endpoint reference its provider by
        name. The post-migration shape matches what the editor
        writes, so a freshly-seeded profile opens cleanly in the
        new "Model Configuration" tab.
        """
        providers: dict[str, Provider] = {}
        endpoints: dict[str, ModelEndpoint] = {}
        bind_address = "127.0.0.1"
        port = 3001
        config_dir = ""
        default_endpoint = ""
        if self._legacy_proxy_config_provider is not None:
            try:
                cfg = self._legacy_proxy_config_provider()
                raw_endpoints = dict(getattr(cfg, "model_endpoints", {}) or {})
                # Build provider records first (one per unique URL).
                # ``_url_to_provider_name`` derives a stable name from
                # the URL host so two URLs that happen to share a host
                # collapse into a single provider (good — they
                # probably are the same service).
                url_to_name: dict[str, str] = {}
                for url in raw_endpoints.values():
                    if not url:
                        continue
                    url_s = str(url)
                    if url_s not in url_to_name:
                        url_to_name[url_s] = _url_to_provider_name(
                            url_s, taken=set(providers.keys())
                        )
                        providers[url_to_name[url_s]] = Provider(
                            name=url_to_name[url_s],
                            url=url_s,
                        )
                # Now build endpoint records that name their provider.
                for model, url in raw_endpoints.items():
                    if not url:
                        continue
                    url_s = str(url)
                    endpoints[str(model)] = ModelEndpoint(
                        provider_name=url_to_name[url_s],
                    )
                bind_address = str(getattr(cfg, "host", "127.0.0.1") or "127.0.0.1")
                try:
                    port = int(getattr(cfg, "port", 3001) or 3001)
                except (TypeError, ValueError):
                    port = 3001
                config_dir = str(getattr(cfg, "config_dir", "") or "")
                default_endpoint = str(getattr(cfg, "default_endpoint", "") or "")
            except (KeyError, TypeError, ValueError, AttributeError):
                providers = {}
                endpoints = {}
        default_kwargs: dict = {
            "name": DEFAULT_PROFILE_NAME,
            "slug": slugify(DEFAULT_PROFILE_NAME),
            "providers": providers,
            "model_endpoints": endpoints,
            "bind_address": bind_address,
            "port": port,
            "config_dir": config_dir,
            "is_default": True,
        }
        # ``default_endpoint`` isn't a first-class profile field yet —
        # the proxy still reads it from ProxyConfig (the live migration
        # to per-profile default_endpoint is a future step). Stash it
        # as a Claude Code settings key for now so the value isn't
        # silently dropped.
        if default_endpoint:
            default_kwargs["claude_settings"] = {
                "proxy_default_endpoint": default_endpoint,
            }
        default = ClaudeProfile(**default_kwargs)
        return {
            "schema_version": SCHEMA_VERSION,
            "active_profile": default.name,
            "profiles": [_serialize_profile(default)],
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
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping corrupt profile entry: %s", exc)
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

    @staticmethod
    def _ensure_default_present(
        profiles: list[ClaudeProfile],
    ) -> list[ClaudeProfile]:
        """Return ``profiles`` with the Default profile at index 0.

        If no Default profile is on disk, create a fresh one with
        dataclass defaults and prepend it. The Default profile is
        the template for new profiles and the always-visible anchor
        of the profile list — it must always be present, and it
        must be at the top so the UI doesn't have to special-case
        ordering.
        """
        for i, p in enumerate(profiles):
            if p.name == DEFAULT_PROFILE_NAME:
                # Move to the front; preserve the rest of the order.
                if i != 0:
                    profiles = [p, *profiles[:i], *profiles[i + 1:]]
                # ``is_default`` may have been forgotten on a manual
                # JSON edit; re-assert it here so the Manage Profiles
                # dialog can use the flag reliably.
                if not getattr(p, "is_default", False):
                    p = copy.copy(p)
                    p.is_default = True
                    profiles[0] = p
                return profiles
        # No Default on disk — fabricate one with defaults.
        default = ClaudeProfile(
            name=DEFAULT_PROFILE_NAME,
            slug=slugify(DEFAULT_PROFILE_NAME),
            is_default=True,
        )
        return [default, *profiles]

    def export_profile(self, name: str) -> dict | None:
        """Export a profile as a shareable dict (credentials stripped).

        Returns None if no profile with the given name exists.
        """
        profiles = self.load()
        target = next((p for p in profiles if p.name == name), None)
        if target is None:
            return None
        data = _serialize_profile(target)
        for prov in (data.get("providers") or {}).values():
            if isinstance(prov, dict):
                prov.pop("key_ref", None)
        data.pop("is_default", None)
        return data

    def import_profile(self, data: dict) -> ClaudeProfile:
        """Import a profile from an exported dict.

        Creates the profile (with a deduplicated name if needed) and
        persists it. Credentials are NOT imported — the user must set
        them separately via the profile editor.

        Returns the imported ClaudeProfile.
        """
        profile = _deserialize_profile(data)
        existing_names = {p.name for p in self.load()}
        if profile.name in existing_names:
            suffix = 2
            base = profile.name
            while f"{base} ({suffix})" in existing_names:
                suffix += 1
            profile.name = f"{base} ({suffix})"
        profile.slug = slugify(profile.name)
        self.upsert(profile)
        return profile

    @property
    def path(self) -> Path:
        return self._path


# ---- (de)serialisation helpers --------------------------------------------


def _url_to_provider_name(url: str, *, taken: set[str]) -> str:
    """Derive a stable provider name from a URL.

    Used by :meth:`ProfileStore._seed_from_legacy_or_default` to
    collapse legacy ``{model: url}`` records into named providers.
    The host portion of the URL is preferred (``api.anthropic.com``
    → ``"Anthropic"`` is too speculative, so we use ``"api-anthropic-com"``
    — a slug the user can rename in the editor if they like). When
    the URL can't be parsed we fall back to a hash-derived slug so
    the name is still stable and unique.

    ``taken`` is the set of provider names already in use so the
    returned name is unique within the seeding pass — collisions get
    a numeric suffix.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    host = (parsed.hostname if parsed else "") or ""
    if host:
        # ``api.anthropic.com`` → ``api-anthropic-com``. Lowercase +
        # dashes; collapse repeated dashes.
        import re as _re
        slug = _re.sub(r"[^a-z0-9-]+", "-", host.lower())
        slug = _re.sub(r"-+", "-", slug).strip("-") or "provider"
    else:
        # URL had no recognisable host (e.g. local path) — use the
        # URL itself, slugified.
        import re as _re
        slug = _re.sub(r"[^a-z0-9-]+", "-", url.lower())
        slug = _re.sub(r"-+", "-", slug).strip("-") or "provider"
    base = slug
    n = 2
    candidate = base
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _serialize_profile(p: ClaudeProfile) -> dict:
    d = asdict(p)
    # ``asdict`` recursively converts Subagent + ModelEndpoint +
    # Provider dataclasses too — drop None fields on each so the JSON
    # stays tidy.
    subagents = []
    for sa in d.get("subagents", []):
        if not isinstance(sa, dict):
            continue
        sa = {k: v for k, v in sa.items() if v is not None}
        subagents.append(sa)
    d["subagents"] = subagents
    raw_providers = d.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raw_providers = {}
    providers_out: dict[str, dict] = {}
    for name, prov in raw_providers.items():
        if isinstance(prov, dict):
            # ``key_ref`` is a string-or-None optional field; we drop
            # None to keep the JSON tidy. ``available_models`` is a
            # tuple-or-omitted optional; drop empty tuples too — the
            # ``available_models is None or available_models == ()``
            # check above already filtered them in the dataclass,
            # but a defensive drop doesn't hurt here.
            prov_d = {
                k: v
                for k, v in prov.items()
                if v is not None and v != ()
            }
        else:
            continue
        # ``name`` + ``url`` are required; ``name`` is the dict key.
        if prov_d.get("url"):
            providers_out[str(name)] = prov_d
    d["providers"] = providers_out
    raw_endpoints = d.get("model_endpoints") or {}
    if not isinstance(raw_endpoints, dict):
        raw_endpoints = {}
    endpoints_out: dict[str, dict] = {}
    for model, ep in raw_endpoints.items():
        if isinstance(ep, dict):
            # ``ModelEndpoint`` carries only ``provider_name`` +
            # ``aliases`` in the post-refactor shape. ``key_ref`` is
            # now a property of the *Provider*, not the endpoint, so
            # we drop it on write. ``aliases`` is dropped when empty
            # so profiles that don't use them stay tidy.
            ep_d = {k: v for k, v in ep.items() if v is not None and v != ()}
            ep_d.pop("key_ref", None)  # legacy: was attached per-endpoint
        else:
            ep_d = {"url": str(ep)}
        # ``provider_name`` is required (or legacy ``url``); ``aliases``
        # is optional and dropped when empty. ``url`` is only retained
        # for the legacy shape — the new shape uses ``provider_name``.
        if "provider_name" in ep_d:
            if ep_d.get("provider_name"):
                endpoints_out[str(model)] = ep_d
        elif ep_d.get("url"):
            endpoints_out[str(model)] = ep_d
    d["model_endpoints"] = endpoints_out
    # Drop empty optional fields so the JSON doesn't carry noise.
    if d.get("default_model") is None:
        d.pop("default_model", None)
    if d.get("session_cwd") is None:
        d.pop("session_cwd", None)
    # Keep the proxy-side fields only when they differ from the
    # dataclass defaults — most profiles will inherit them and the
    # file stays compact.
    if d.get("bind_address") == "127.0.0.1":
        d.pop("bind_address", None)
    if d.get("port") == 3001:
        d.pop("port", None)
    if not d.get("config_dir"):
        d.pop("config_dir", None)
    if d.get("is_default") is False:
        d.pop("is_default", None)
    return d


def _deserialize_profile(d: dict) -> ClaudeProfile:
    name = str(d.get("name") or "").strip()
    if not name:
        raise ValueError("profile missing name")
    slug = str(d.get("slug") or "").strip() or slugify(name)
    raw_providers = d.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raw_providers = {}
    providers: dict[str, Provider] = {}
    for prov_name, raw in raw_providers.items():
        if not isinstance(raw, dict):
            continue
        prov_url = raw.get("url")
        if not prov_url:
            continue
        # The key in the dict IS the provider's stable name. If the
        # inner ``name`` field disagrees, prefer the key (it's the
        # canonical identifier and the dict key under which endpoints
        # will look the provider up).
        # ``key_ref`` is the CredentialStore ref the proxy uses to
        # authenticate requests routed through this provider. ``None``
        # means "no proxy-side auth" — the Anthropic path forwards
        # the client's ``x-api-key``; the OpenAI path falls back to
        # its configured ``OPENAI_API_KEY``.
        raw_key_ref = raw.get("key_ref")
        key_ref = str(raw_key_ref) if raw_key_ref is not None else None
        # ``available_models`` may be either a JSON list of strings,
        # a single comma-separated string, or a tuple of strings (the
        # shape :func:`dataclasses.asdict` produces from the dataclass
        # default). All three round-trip; we normalise to a tuple of
        # stripped, deduped strings.
        am_raw = raw.get("available_models")
        if isinstance(am_raw, (list, tuple)):
            available_models = tuple(
                dict.fromkeys(
                    str(m).strip() for m in am_raw if str(m).strip()
                )
            )
        elif isinstance(am_raw, str):
            available_models = tuple(
                dict.fromkeys(
                    s.strip() for s in am_raw.split(",") if s.strip()
                )
            )
        else:
            available_models = ()
        providers[str(prov_name)] = Provider(
            name=str(prov_name),
            url=str(prov_url),
            key_ref=key_ref,
            available_models=available_models,
        )
    raw_endpoints = d.get("model_endpoints") or {}
    if not isinstance(raw_endpoints, dict):
        raw_endpoints = {}
    # Accept four shapes:
    #   - new (post-refactor): {"model": {"provider_name": "...",
    #     "aliases": [...]}}
    #   - transitional: {"model": {"provider_name": "..."}} (no aliases)
    #   - legacy with per-endpoint key_ref: {"model": {"url": "...",
    #     "key_ref": "..."}}
    #   - legacy flat-string: {"model": "https://..."} (pre-ModelEndpoint)
    #
    # Note: per-endpoint ``key_ref`` is no longer a thing post-refactor
    # (the key lives on the provider now). The deserializer below just
    # ignores ``key_ref`` on the endpoint — the provider's ``key_ref``
    # is the auth source.
    endpoints: dict[str, ModelEndpoint] = {}
    for model, raw in raw_endpoints.items():
        if isinstance(raw, dict):
            provider_name = raw.get("provider_name") or ""
            # Aliases may be either a JSON list of strings, a single
            # comma-separated string, or a tuple of strings (the
            # shape :func:`dataclasses.asdict` produces from the
            # dataclass default). All three round-trip; we
            # normalise to a tuple of stripped, deduped strings.
            aliases_raw = raw.get("aliases")
            if isinstance(aliases_raw, (list, tuple)):
                aliases = tuple(
                    dict.fromkeys(
                        str(a).strip() for a in aliases_raw if str(a).strip()
                    )
                )
            elif isinstance(aliases_raw, str):
                aliases = tuple(
                    dict.fromkeys(
                        s.strip() for s in aliases_raw.split(",") if s.strip()
                    )
                )
            else:
                aliases = ()
            if provider_name:
                endpoints[str(model)] = ModelEndpoint(
                    provider_name=str(provider_name),
                    aliases=aliases,
                )
            else:
                url = raw.get("url")
                if not url:
                    continue
                # Older shape: endpoints carried a literal URL. Treat
                # that URL's host portion as the provider name (and
                # auto-create the provider on the fly) so the profile
                # round-trips without throwing away the routing info.
                # The on-disk providers dict is still empty — the
                # editor will surface this and let the user re-attach
                # the endpoint to a named provider on first edit.
                endpoints[str(model)] = ModelEndpoint(
                    provider_name="",
                    aliases=aliases,
                )
        elif raw:
            # Legacy flat-string: bare URL. Same shape as above — the
            # endpoint carries an empty ``provider_name`` until the
            # user attaches it on first edit.
            endpoints[str(model)] = ModelEndpoint(provider_name="")
    default_model = d.get("default_model")
    if default_model is not None:
        default_model = str(default_model) or None
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
    bind_address = d.get("bind_address")
    if bind_address is not None:
        bind_address = str(bind_address) or "127.0.0.1"
    else:
        bind_address = "127.0.0.1"
    raw_port = d.get("port")
    try:
        port = int(raw_port) if raw_port is not None else 3001
    except (TypeError, ValueError):
        port = 3001
    config_dir = d.get("config_dir")
    if config_dir is not None:
        config_dir = str(config_dir) or ""
    else:
        config_dir = ""
    is_default = bool(d.get("is_default", False))
    return ClaudeProfile(
        name=name,
        slug=slug,
        providers=providers,
        model_endpoints=endpoints,
        default_model=default_model,
        env_vars=env_vars,
        claude_settings=settings_raw,
        subagents=subagents,
        session_cwd=session_cwd,
        bind_address=bind_address,
        port=port,
        config_dir=config_dir,
        is_default=is_default,
    )