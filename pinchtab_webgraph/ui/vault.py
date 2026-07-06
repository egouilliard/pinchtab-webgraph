#!/usr/bin/env python3
"""Encrypted-at-rest credentials vault feeding the keyring-backed login path.

This is the Phase-2 write side of the OPTIONAL web UI. It owns two split stores,
mirroring `login.py`'s hard rule that nothing secret is ever written to disk here:

  * ``login-config.json``  — per-host ROUTING only (url/username + optional selector
    overrides), written atomically with mode 0600. NEVER a password.
  * the PASSWORD            — the OS KEYRING ONLY, via ``keyring.set_password`` — the
    exact (service, username) pair ``login._get_password`` reads back at crawl time.

Pure logic, NO fastapi import — the UI server (server.py) is the only HTTP binding.
Like `login._get_password`, ``keyring`` is imported LAZILY inside each function that
needs it, so the base package stays a pure-stdlib install; a missing package or
backend degrades to a structured ``VaultUnavailable`` rather than a crash.

SECURITY INVARIANT: the literal password value flows in ONLY as a function argument
and out ONLY through the single ``keyring.set_password`` call. It never appears in a
returned dict, a log line, an exception message, or the on-disk config — every read
surface exposes a ``has_password`` boolean, never the secret.
"""
import json
import os

from .. import login, cache_store

DEFAULT_SERVICE = login.DEFAULT_SERVICE

# The 5 optional per-host routing overrides `login.py` understands beyond url/username.
_OPTIONAL_FIELDS = ("userField", "passField", "submit", "successUrl", "keyringService")


class VaultUnavailable(Exception):
    """Keyring is unusable — the package is absent, or no backend is configured.

    ``reason`` is one of {"no_keyring_package", "no_keyring_backend"}; ``detail`` is a
    human remedy hint. Callers turn this into a structured status, never a 500.
    """

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason = reason
        self.detail = detail


def config_path():
    return os.path.join(cache_store.home_dir(), "login-config.json")


def _load():
    if not os.path.exists(config_path()):
        return {}
    return login.load_config(config_path())


def _atomic_write(cfg):
    # ATOMIC + 0600: write to <config>.tmp, tighten its mode BEFORE it holds routing,
    # then os.replace onto the target so a reader never sees a half-written file.
    # (mirrors cache_store.atomic_write, plus the chmod login.py's split-store demands.)
    os.makedirs(cache_store.home_dir(), exist_ok=True)
    target = config_path()
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _host_key(host_or_url):
    # keep in sync with login.py:_host
    return login._host(host_or_url)


def backend_status():
    """NEVER raises. {"available":True,"backend":...} or the structured unavailable dict.

    A read-only probe (get_password on a throwaway key) distinguishes a real backend
    from keyring's fail backend — which is installed when NO usable OS backend exists
    and raises NoKeyringError on every operation.
    """
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return {"available": False, "reason": "no_keyring_package",
                "detail": _NO_PACKAGE_HINT}
    try:
        backend = keyring.get_keyring()
        keyring.get_password("__pinchtab_webgraph_probe__", "__pinchtab_webgraph_probe__")
    except keyring.errors.NoKeyringError:
        return {"available": False, "reason": "no_keyring_backend",
                "detail": _NO_BACKEND_HINT}
    return {"available": True, "backend": type(backend).__name__}


_NO_PACKAGE_HINT = (
    "The optional 'keyring' package is not installed. Install the UI extra "
    "(pip install 'pinchtab-webgraph[ui]') or 'pip install keyring', or skip the "
    "vault and log in by hand once in the persistent bridge profile.")
_NO_BACKEND_HINT = (
    "No usable OS keyring backend is configured. Install one, e.g. "
    "'pip install keyrings.alt' for a file backend, or use the hand-login path "
    "(log in once in the persistent bridge profile) instead of the vault.")


def _has_password(entry):
    """bool | None — whether a secret exists for this entry. NEVER returns the value."""
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return None
    service = entry.get("keyringService", DEFAULT_SERVICE)
    user = entry.get("username", "")
    try:
        pw = keyring.get_password(service, user)
    except keyring.errors.NoKeyringError:
        return None
    return bool(pw)


def _mask(host, entry):
    """The masked, password-free view of one routing entry (built field-by-field)."""
    return {
        "host": host,
        "url": entry.get("url"),
        "username": entry.get("username"),
        "has_password": _has_password(entry),
        "userField": entry.get("userField"),
        "passField": entry.get("passField"),
        "submit": entry.get("submit"),
        "successUrl": entry.get("successUrl"),
        "keyringService": entry.get("keyringService"),
    }


def list_credentials():
    return {"config_path": config_path(),
            "credentials": [_mask(h, e) for h, e in _load().items()]}


def get_routing(host):
    cache_store.validate_host(host)
    cfg = _load()
    key = _host_key(host)
    for h, e in cfg.items():
        if _host_key(h) == key:
            return _mask(h, e)
    return None


def set_credential(host, *, url, username, password, userField=None, passField=None,
                   submit=None, successUrl=None, keyringService=None):
    """Write the secret to the keyring FIRST, then the routing to login-config.json.

    Returns the masked (password-free) view. ValueError on a bad host token or a
    missing required field; VaultUnavailable when the keyring can't hold the secret.
    """
    cache_store.validate_host(host)  # ValueError on bad token — let it propagate

    missing = [name for name, val in
               (("url", url), ("username", username), ("password", password))
               if not val]
    if missing:
        raise ValueError("missing required field(s): %s" % ", ".join(missing))

    service = keyringService or DEFAULT_SERVICE

    # SECRET WRITTEN FIRST — so routing on disk never points at an absent secret.
    try:
        import keyring
        import keyring.errors
    except ImportError:
        raise VaultUnavailable("no_keyring_package", _NO_PACKAGE_HINT)
    try:
        keyring.set_password(service, username, password)
    except keyring.errors.NoKeyringError:
        raise VaultUnavailable("no_keyring_backend", _NO_BACKEND_HINT)

    cfg = _load()
    entry = {"url": url, "username": username}
    for name, val in (("userField", userField), ("passField", passField),
                      ("submit", submit), ("successUrl", successUrl),
                      ("keyringService", keyringService)):
        if val is not None:
            entry[name] = val
    cfg[_host_key(host)] = entry
    _atomic_write(cfg)

    return _mask(_host_key(host), entry)


def delete_credential(host, *, delete_secret=True):
    """Remove one host's routing (and, by default, its keyring secret). Idempotent."""
    cache_store.validate_host(host)
    cfg = _load()
    key = _host_key(host)
    entry = cfg.pop(key, None)
    routing_removed = entry is not None

    secret_removed = False
    if delete_secret and entry and entry.get("username"):
        service = entry.get("keyringService", DEFAULT_SERVICE)
        user = entry.get("username")
        try:
            import keyring
            import keyring.errors
        except ImportError:
            secret_removed = None
        else:
            try:
                keyring.delete_password(service, user)
                secret_removed = True
            except keyring.errors.PasswordDeleteError:
                secret_removed = False  # already absent — best-effort, idempotent
            except keyring.errors.NoKeyringError:
                secret_removed = None

    if routing_removed:
        _atomic_write(cfg)

    return {"host": key, "routing_removed": routing_removed,
            "secret_removed": secret_removed}
