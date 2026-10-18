import stat

from linuxdo_mcp import session, store

from .conftest import FakeResponse


def test_no_session_means_anonymous():
    assert session.get_cookie() == ""


def test_saved_session_wins_over_env(monkeypatch):
    session.save("_t=saved")
    monkeypatch.setenv("LINUXDO_COOKIE", "_t=env")
    assert session.get_cookie() == "_t=saved"


def test_env_bootstraps_and_bare_token_is_normalized(monkeypatch):
    monkeypatch.setenv("LINUXDO_COOKIE", "abc")
    assert session.get_cookie() == "_t=abc"
    assert store.read_json(session.FILE)["cookie"] == "_t=abc"


def test_session_file_is_private(isolated_cache):
    session.save("_t=x")
    mode = stat.S_IMODE((isolated_cache / session.FILE).stat().st_mode)
    assert mode == 0o600


def test_rotation_keeps_extra_fields():
    session.save("_t=old", username="alice")
    session.absorb_rotation(FakeResponse(cookies={"_t": "new"}))
    data = store.read_json(session.FILE)
    assert data["cookie"] == "_t=new"
    assert data["username"] == "alice"


def test_rotation_ignores_cleared_cookie():
    session.save("_t=old")
    session.absorb_rotation(FakeResponse(cookies={"_t": ""}))
    assert session.get_cookie() == "_t=old"


def test_clear():
    session.save("_t=x")
    session.clear()
    assert session.get_cookie() == ""


def test_logout_is_not_undone_by_env(monkeypatch):
    monkeypatch.setenv("LINUXDO_COOKIE", "_t=stale-env")
    assert session.get_cookie() == "_t=stale-env"
    session.clear()
    assert session.get_cookie() == ""


def test_set_username_keeps_rotated_cookie():
    session.save("_t=rotated")
    session.set_username("alice")
    assert session.get_cookie() == "_t=rotated"
    assert store.read_json(session.FILE)["username"] == "alice"


def test_legacy_shared_cookie_file_is_removed(isolated_cache):
    (isolated_cache / session.LEGACY_FILE).write_text('{"cookie": "_t=shared"}')
    session.save("_t=independent")
    assert not (isolated_cache / session.LEGACY_FILE).exists()


def test_cache_dir_is_private(isolated_cache):
    session.save("_t=x")
    assert stat.S_IMODE(isolated_cache.stat().st_mode) == 0o700
    assert not list(isolated_cache.glob("*.tmp"))
