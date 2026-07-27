"""Plaintext JSON credential store with chmod 600.

Stores API keys keyed by a stable ``ref`` string (e.g. ``"default-key"``).
The file lives in the app data dir so it's per-user.

Security posture (v1)
---------------------
- **Plaintext on disk** — chosen over the system keyring because
  WSL2 / headless Linux often lacks a usable Secret Service, and the
  goal of v1 is to ship working per-profile credentials. Encrypting
  with a derived key would still leave a single point of failure
  (the machine identifier), so the marginal security improvement
  over plaintext + ``chmod 600`` is small for a local dev tool.
- **``chmod 600`` enforced on every write** — every other user on the
  box is denied read access even though the file is plaintext.
- **Never logged, never serialised into ``profiles.json``** — the
  store is the single source of truth for keys.

When upgrading to keyring, replace this module's body but keep the
public API (``get``/``set``/``delete``/``all_refs``) — the only
caller is :class:`ProfileStore` and :func:`build_claude_session_env`.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("sharkdrown.credentials")

_IS_POSIX = sys.platform != "win32"
_WINDOWS_CHMOD_WARNING_ISSUED = False


class CredentialStore:
    """Per-user API key store backed by a chmod-600 JSON file.

    File format::

        {
          "schema_version": 1,
          "credentials": {
            "<ref>": "<plaintext key>",
            ...
          }
        }

    Unknown top-level keys are preserved across saves (forward-compat
    with future credential backends). A corrupt file is treated as
    empty — better than refusing to start when a user accidentally
    edits it by hand.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        # Ensure parent exists; we write the file lazily.
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ---- public API --------------------------------------------------------

    def get(self, ref: str) -> str | None:
        """Return the key for ``ref`` or ``None`` if absent."""
        data = self._read()
        creds = data.get("credentials", {})
        if not isinstance(creds, dict):
            return None
        val = creds.get(ref)
        return val if isinstance(val, str) else None

    def set(self, ref: str, value: str) -> None:
        """Insert or update ``ref`` → ``value``. Empty string deletes.

        Strip leading/trailing whitespace from the value before storing.
        A copy-paste from a key-management page often lands with a
        trailing newline; OpenRouter's format validator rejects those
        with a 401 "Missing Authentication header" before the auth
        path even runs, so silently dropping the newline is the
        least-surprising fix. Whitespace-only values are treated as
        empty (deletion).
        """
        # Strip all whitespace (spaces, tabs, newlines, carriage
        # returns — the full Unicode set since some upstream
        # dashboards use non-breaking spaces). ``.strip()`` is
        # sufficient for our threat model: keys are a single token
        # with no legitimate internal whitespace.
        cleaned = value.strip()
        # Drop any internal whitespace too: a paste that landed with
        # a literal newline mid-string would still be rejected as
        # malformed. ``split()`` with no args collapses any run of
        # whitespace and rejoins with a single space, which for a
        # key token is the same as "no whitespace at all".
        cleaned = "".join(cleaned.split())
        data = self._read()
        creds = data.setdefault("credentials", {})
        if not isinstance(creds, dict):
            creds = {}
            data["credentials"] = creds
        if cleaned == "":
            creds.pop(ref, None)
        else:
            creds[ref] = cleaned
        self._write(data)

    def delete(self, ref: str) -> None:
        """Remove ``ref`` if present (silent if absent)."""
        data = self._read()
        creds = data.get("credentials", {})
        if isinstance(creds, dict) and ref in creds:
            creds.pop(ref)
            self._write(data)

    def all_refs(self) -> dict[str, str]:
        """Return a copy of the full credentials mapping."""
        data = self._read()
        creds = data.get("credentials", {})
        if not isinstance(creds, dict):
            return {}
        return {k: v for k, v in creds.items() if isinstance(k, str) and isinstance(v, str)}

    def refs(self) -> Iterable[str]:
        """Return the set of stored credential refs (no values)."""
        return self.all_refs().keys()

    # ---- internals ---------------------------------------------------------

    def _read(self) -> dict:
        if not self._path.is_file():
            return {"schema_version": self.SCHEMA_VERSION, "credentials": {}}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {"schema_version": self.SCHEMA_VERSION, "credentials": {}}
            return data
        except (OSError, json.JSONDecodeError):
            return {"schema_version": self.SCHEMA_VERSION, "credentials": {}}

    def _write(self, data: dict) -> None:
        global _WINDOWS_CHMOD_WARNING_ISSUED  # noqa: PLW0603

        data["schema_version"] = self.SCHEMA_VERSION
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

        if _IS_POSIX:
            os.chmod(tmp, 0o600)
        elif not _WINDOWS_CHMOD_WARNING_ISSUED:
            logger.warning(
                "credential store on Windows has no file-permission protection; "
                "ensure %s is in a user-private directory",
                self._path,
            )
            _WINDOWS_CHMOD_WARNING_ISSUED = True

        os.replace(tmp, self._path)

        if _IS_POSIX:
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        """The on-disk path of the credential store (useful for tests)."""
        return self._path

    @property
    def mode(self) -> int:
        """Stat mode of the file (0 if missing). Useful for tests."""
        try:
            return stat.S_IMODE(self._path.stat().st_mode)
        except OSError:
            return 0