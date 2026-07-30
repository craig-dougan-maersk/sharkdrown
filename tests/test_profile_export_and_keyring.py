"""Tests for ProfileStore.export_profile/import_profile and
CredentialStore keyring backend."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "src"))


def _tmp(suffix: str = "") -> Path:
    d = Path(tempfile.mkdtemp())
    return d / f"test{suffix}.json"


# ---------------------------------------------------------------------------
# ProfileStore.export_profile / import_profile
# ---------------------------------------------------------------------------


def test_export_profile_returns_none_for_missing_name() -> None:
    from src.profiles import ProfileStore

    store = ProfileStore(path=_tmp())
    store.load()
    assert store.export_profile("nonexistent") is None


def test_export_profile_strips_key_refs() -> None:
    from src.profiles import ClaudeProfile, ProfileStore
    from src.profiles.models import Provider

    path = _tmp()
    store = ProfileStore(path=path)
    profile = store.load()[0]  # Default
    profile.providers = {
        "anthropic": Provider(
            name="anthropic",
            url="https://api.anthropic.com",
            key_ref="default-key",
        )
    }
    store.upsert(profile)

    exported = store.export_profile("Default")
    assert exported is not None
    # key_ref must be stripped from every provider in the export
    for prov in (exported.get("providers") or {}).values():
        assert "key_ref" not in prov, f"key_ref leaked into export: {prov}"


def test_export_profile_omits_is_default_flag() -> None:
    from src.profiles import ProfileStore

    store = ProfileStore(path=_tmp())
    store.load()
    exported = store.export_profile("Default")
    assert exported is not None
    assert "is_default" not in exported


def test_export_profile_roundtrips_name_and_endpoints() -> None:
    from src.profiles import ClaudeProfile, ProfileStore
    from src.profiles.models import ModelEndpoint, Provider

    path = _tmp()
    store = ProfileStore(path=path)
    profile = store.load()[0]
    profile.providers = {
        "openai": Provider(name="openai", url="https://api.openai.com"),
    }
    profile.model_endpoints = {
        "gpt-4o": ModelEndpoint(provider_name="openai"),
    }
    store.upsert(profile)

    exported = store.export_profile("Default")
    assert exported is not None
    assert exported["name"] == "Default"
    assert "gpt-4o" in (exported.get("model_endpoints") or {})


def test_import_profile_creates_new_profile() -> None:
    from src.profiles import ProfileStore

    path = _tmp()
    store = ProfileStore(path=path)
    store.load()

    exported = store.export_profile("Default")
    assert exported is not None
    exported["name"] = "ImportedProfile"

    imported = store.import_profile(exported)
    assert imported.name == "ImportedProfile"

    names = [p.name for p in store.load()]
    assert "ImportedProfile" in names


def test_import_profile_deduplicates_name_on_collision() -> None:
    from src.profiles import ProfileStore

    path = _tmp()
    store = ProfileStore(path=path)
    store.load()

    exported = store.export_profile("Default")
    assert exported is not None

    # Import twice — second import must get a suffixed name
    first = store.import_profile({**exported, "name": "Duplicate"})
    assert first.name == "Duplicate"

    second = store.import_profile({**exported, "name": "Duplicate"})
    assert second.name == "Duplicate (2)"


def test_import_profile_does_not_carry_credentials_from_export() -> None:
    """export_profile strips key_refs, so a normal export→import cycle
    produces a profile with no credential references."""
    from src.profiles import ProfileStore
    from src.profiles.models import Provider

    path = _tmp()
    store = ProfileStore(path=path)
    profile = store.load()[0]
    profile.providers = {
        "anthropic": Provider(
            name="anthropic",
            url="https://api.anthropic.com",
            key_ref="some-sensitive-key-ref",
        )
    }
    store.upsert(profile)

    exported = store.export_profile("Default")
    assert exported is not None

    # The export must already have no key_ref — this is what makes the
    # round-trip safe. import_profile operates on what export gives it.
    for prov in (exported.get("providers") or {}).values():
        assert "key_ref" not in prov

    exported["name"] = "Imported"
    imported = store.import_profile(exported)
    for prov in (imported.providers or {}).values():
        assert not prov.key_ref, f"key_ref present after clean import: {prov.key_ref!r}"


def test_import_export_roundtrip_preserves_endpoints() -> None:
    from src.profiles import ProfileStore
    from src.profiles.models import ModelEndpoint, Provider

    path = _tmp()
    store = ProfileStore(path=path)
    profile = store.load()[0]
    profile.providers = {
        "openai": Provider(name="openai", url="https://api.openai.com"),
    }
    profile.model_endpoints = {
        "gpt-4o": ModelEndpoint(provider_name="openai"),
        "gpt-4-turbo": ModelEndpoint(provider_name="openai"),
    }
    store.upsert(profile)

    exported = store.export_profile("Default")
    assert exported is not None
    exported["name"] = "Roundtripped"

    imported = store.import_profile(exported)
    assert set(imported.model_endpoints or {}) == {"gpt-4o", "gpt-4-turbo"}


# ---------------------------------------------------------------------------
# CredentialStore — keyring backend
# ---------------------------------------------------------------------------


def _keyring_mocks() -> tuple[MagicMock, dict[str, str]]:
    """Return (mock_keyring_module, in_memory_store) for patching."""
    store: dict[str, str] = {}
    kr = MagicMock()

    def _get_password(service, ref):
        return store.get(ref)

    def _set_password(service, ref, value):
        store[ref] = value

    def _delete_password(service, ref):
        store.pop(ref, None)

    def _get_credential(service, ref):
        return None  # probe returns None → available

    kr.get_password.side_effect = _get_password
    kr.set_password.side_effect = _set_password
    kr.get_credential.side_effect = _get_credential
    kr.errors = MagicMock()
    kr.errors.PasswordDeleteError = KeyError
    kr.delete_password.side_effect = _delete_password

    return kr, store


def test_keyring_backend_set_and_get() -> None:
    from src.profiles.credential_store import CredentialStore, _KeyringBackend

    backing: dict[str, str] = {}
    cs = CredentialStore(path=_tmp())
    cs._keyring = MagicMock(spec=_KeyringBackend)
    cs._keyring.get.side_effect = lambda ref: backing.get(ref)
    cs._keyring.set.side_effect = lambda ref, val: backing.update({ref: val}) if val else backing.pop(ref, None)

    cs.set("my-ref", "sk-test-12345")
    assert cs.get("my-ref") == "sk-test-12345"


def test_keyring_backend_sentinel_written_to_file() -> None:
    from src.profiles.credential_store import (
        _KEYRING_SENTINEL,
        CredentialStore,
        _KeyringBackend,
    )

    backing: dict[str, str] = {}
    path = _tmp()

    cs = CredentialStore(path=path)
    cs._keyring = MagicMock(spec=_KeyringBackend)
    cs._keyring.get.side_effect = lambda ref: backing.get(ref)
    cs._keyring.set.side_effect = lambda ref, val: backing.update({ref: val}) if val else backing.pop(ref, None)

    cs.set("some-key", "real-secret")
    # The JSON file must hold the sentinel, not the plaintext value
    import json
    data = json.loads(path.read_text())
    assert data["credentials"]["some-key"] == _KEYRING_SENTINEL
    # get() should resolve via keyring, not the sentinel
    assert cs.get("some-key") == "real-secret"


def test_keyring_backend_delete_clears_both_file_and_keyring() -> None:
    from src.profiles.credential_store import (
        _KEYRING_SENTINEL,
        CredentialStore,
        _KeyringBackend,
    )
    import json

    backing: dict[str, str] = {}
    path = _tmp()

    cs = CredentialStore(path=path)
    cs._keyring = MagicMock(spec=_KeyringBackend)
    cs._keyring.get.side_effect = lambda ref: backing.get(ref)
    cs._keyring.set.side_effect = lambda ref, val: backing.update({ref: val}) if val else backing.pop(ref, None)
    cs._keyring.delete.side_effect = lambda ref: backing.pop(ref, None)

    cs.set("del-ref", "secret")
    assert cs.get("del-ref") == "secret"

    cs.delete("del-ref")
    assert cs.get("del-ref") is None
    # Sentinel must be gone from the file too
    if path.exists():
        data = json.loads(path.read_text())
        assert "del-ref" not in data.get("credentials", {})


def test_keyring_all_refs_resolves_sentinels() -> None:
    """all_refs() should resolve sentinel values via the keyring backend."""
    from src.profiles.credential_store import CredentialStore, _KeyringBackend

    backing = {"ref-a": "val-a", "ref-b": "val-b"}
    path = _tmp()

    cs = CredentialStore(path=path)
    cs._keyring = MagicMock(spec=_KeyringBackend)
    cs._keyring.get.side_effect = lambda ref: backing.get(ref)
    cs._keyring.set.side_effect = lambda ref, val: backing.update({ref: val}) if val else None

    cs.set("ref-a", "val-a")
    cs.set("ref-b", "val-b")

    resolved = cs.all_refs()
    assert resolved == {"ref-a": "val-a", "ref-b": "val-b"}


def test_keyring_all_refs_omits_missing_keys() -> None:
    """all_refs() silently omits a ref whose keyring entry was lost."""
    from src.profiles.credential_store import CredentialStore, _KeyringBackend

    backing: dict[str, str] = {}
    path = _tmp()

    cs = CredentialStore(path=path)
    cs._keyring = MagicMock(spec=_KeyringBackend)
    cs._keyring.get.side_effect = lambda ref: backing.get(ref)
    cs._keyring.set.side_effect = lambda ref, val: backing.update({ref: val}) if val else None

    cs.set("present-ref", "some-value")
    # Simulate keyring backend losing the entry
    backing.pop("present-ref")

    resolved = cs.all_refs()
    assert "present-ref" not in resolved


def test_keyring_availability_fallback() -> None:
    """When keyring is unavailable, CredentialStore falls back to file backend."""
    with patch("src.profiles.credential_store._keyring_available", return_value=False):
        from src.profiles.credential_store import CredentialStore

        cs = CredentialStore(path=_tmp(), use_keyring=True)
        assert cs._keyring is None

    # File backend must still work
    cs.set("plain-ref", "plain-value")
    assert cs.get("plain-ref") == "plain-value"
