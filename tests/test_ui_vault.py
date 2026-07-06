"""Tests for pinchtab_webgraph.ui.vault — the Phase-2 credentials vault.

Guarded by importorskip("keyring") so a base run without the optional dep skips.
A FakeKeyring (in-memory) backend is installed fresh per test so nothing touches a
real OS keyring; isolated_cache_home points login-config.json at a tmp dir.

The load-bearing security assertion recurs: the plaintext password NEVER lands in a
returned dict nor in the on-disk login-config.json — only in the fake keyring store.
"""
import json
import sys

import pytest

keyring = pytest.importorskip("keyring")
import keyring.backend
import keyring.errors

from pinchtab_webgraph.ui import vault

PASSWORD = "sup3r-s3cret-pw"


class FakeKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring backend — no OS interaction, resets per instance."""
    priority = 1

    def __init__(self):
        super().__init__()
        self._store = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture
def fake_keyring():
    kr = FakeKeyring()
    keyring.set_keyring(kr)
    return kr


# --- round-trip + on-disk secrecy --------------------------------------------

def test_set_get_roundtrip_and_no_password_on_disk(isolated_cache_home, fake_keyring):
    host = "app.example.com"
    masked = vault.set_credential(host, url="https://app.example.com/login",
                                  username="me@example.com", password=PASSWORD)
    assert masked["has_password"] is True
    assert masked["url"] == "https://app.example.com/login"
    assert "password" not in masked

    routing = vault.get_routing(host)
    assert routing["host"] == host
    assert routing["username"] == "me@example.com"
    assert routing["has_password"] is True
    assert "password" not in routing

    # RAW on-disk file must carry NO password key.
    on_disk = json.load(open(vault.config_path()))
    assert "password" not in on_disk[host]
    assert on_disk[host]["url"] == "https://app.example.com/login"
    # but the secret IS in the keyring, exactly where login._get_password reads it.
    assert keyring.get_password(vault.DEFAULT_SERVICE, "me@example.com") == PASSWORD


def test_optional_fields_and_custom_service(isolated_cache_home, fake_keyring):
    host = "svc.example.com"
    vault.set_credential(host, url="https://svc.example.com/in", username="u",
                         password=PASSWORD, userField="input#u", successUrl="/home",
                         keyringService="custom-svc")
    on_disk = json.load(open(vault.config_path()))[host]
    assert on_disk["userField"] == "input#u"
    assert on_disk["successUrl"] == "/home"
    assert on_disk["keyringService"] == "custom-svc"
    # secret stored under the custom service, and _has_password reads it back.
    assert keyring.get_password("custom-svc", "u") == PASSWORD
    assert vault.get_routing(host)["has_password"] is True


# --- list ---------------------------------------------------------------------

def test_list_credentials_multiple_masked(isolated_cache_home, fake_keyring):
    vault.set_credential("a.example.com", url="https://a/x", username="a",
                         password=PASSWORD)
    vault.set_credential("b.example.com", url="https://b/x", username="b",
                         password=PASSWORD)
    listing = vault.list_credentials()
    hosts = {c["host"] for c in listing["credentials"]}
    assert hosts == {"a.example.com", "b.example.com"}
    for c in listing["credentials"]:
        assert "password" not in c
        assert c["has_password"] is True
    assert listing["config_path"] == vault.config_path()


# --- delete -------------------------------------------------------------------

def test_delete_removes_routing_and_secret(isolated_cache_home, fake_keyring):
    host = "gone.example.com"
    vault.set_credential(host, url="https://gone/x", username="g", password=PASSWORD)
    res = vault.delete_credential(host, delete_secret=True)
    assert res["routing_removed"] is True
    assert res["secret_removed"] is True
    assert vault.get_routing(host) is None
    assert keyring.get_password(vault.DEFAULT_SERVICE, "g") is None


def test_delete_idempotent_and_keep_secret(isolated_cache_home, fake_keyring):
    host = "keep.example.com"
    vault.set_credential(host, url="https://keep/x", username="k", password=PASSWORD)
    # keep the secret
    res = vault.delete_credential(host, delete_secret=False)
    assert res["routing_removed"] is True
    assert res["secret_removed"] is False
    assert keyring.get_password(vault.DEFAULT_SERVICE, "k") == PASSWORD
    # deleting again: routing already gone — idempotent, no raise.
    res2 = vault.delete_credential(host, delete_secret=True)
    assert res2["routing_removed"] is False


# --- host-key normalization ---------------------------------------------------

def test_host_key_normalization(isolated_cache_home, fake_keyring):
    vault.set_credential("WWW.App.Example.com", url="https://app.example.com/login",
                         username="n", password=PASSWORD)
    r = vault.get_routing("app.example.com")
    assert r is not None
    assert r["host"] == "app.example.com"
    # stored under the normalized key
    assert "app.example.com" in json.load(open(vault.config_path()))


# --- invalid host -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../etc", "bad host", "a/b", ""])
def test_invalid_host_raises(isolated_cache_home, fake_keyring, bad):
    with pytest.raises(ValueError):
        vault.set_credential(bad, url="https://x/y", username="u", password=PASSWORD)
    with pytest.raises(ValueError):
        vault.get_routing(bad)
    with pytest.raises(ValueError):
        vault.delete_credential(bad)


# --- missing required fields --------------------------------------------------

def test_missing_password_raises(isolated_cache_home, fake_keyring):
    with pytest.raises(ValueError):
        vault.set_credential("h.example.com", url="https://h/x", username="u",
                             password="")


def test_missing_username_raises(isolated_cache_home, fake_keyring):
    with pytest.raises(ValueError):
        vault.set_credential("h.example.com", url="https://h/x", username="",
                             password=PASSWORD)


def test_missing_url_raises(isolated_cache_home, fake_keyring):
    with pytest.raises(ValueError):
        vault.set_credential("h.example.com", url="", username="u", password=PASSWORD)


# --- VaultUnavailable: no backend --------------------------------------------

def test_vault_unavailable_no_backend(isolated_cache_home):
    import keyring.backends.fail
    keyring.set_keyring(keyring.backends.fail.Keyring())

    status = vault.backend_status()
    assert status["available"] is False
    assert status["reason"] == "no_keyring_backend"

    with pytest.raises(vault.VaultUnavailable) as ei:
        vault.set_credential("f.example.com", url="https://f/x", username="u",
                             password=PASSWORD)
    assert ei.value.reason == "no_keyring_backend"

    # delete degrades to secret_removed=None (no unguarded exception).
    # First seed a routing entry via a temporary real backend, so there's something
    # to remove; then swap to the fail backend and delete.
    kr = FakeKeyring()
    keyring.set_keyring(kr)
    vault.set_credential("f.example.com", url="https://f/x", username="u",
                         password=PASSWORD)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    res = vault.delete_credential("f.example.com", delete_secret=True)
    assert res["routing_removed"] is True
    assert res["secret_removed"] is None


# --- VaultUnavailable: package not installed ----------------------------------

def test_vault_unavailable_no_package(isolated_cache_home, monkeypatch):
    # Simulate `import keyring` failing by masking the module.
    monkeypatch.setitem(sys.modules, "keyring", None)

    status = vault.backend_status()
    assert status["available"] is False
    assert status["reason"] == "no_keyring_package"

    with pytest.raises(vault.VaultUnavailable) as ei:
        vault.set_credential("p.example.com", url="https://p/x", username="u",
                             password=PASSWORD)
    assert ei.value.reason == "no_keyring_package"


def test_has_password_none_without_backend(isolated_cache_home, fake_keyring):
    # write a routing entry, then read _has_password under the fail backend.
    vault.set_credential("h2.example.com", url="https://h2/x", username="u",
                         password=PASSWORD)
    import keyring.backends.fail
    keyring.set_keyring(keyring.backends.fail.Keyring())
    r = vault.get_routing("h2.example.com")
    assert r["has_password"] is None
