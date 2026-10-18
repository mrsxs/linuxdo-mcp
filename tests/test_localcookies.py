import sqlite3

import pycookiecheat
import pytest

from linuxdo_mcp import localcookies


def make_profile(root, name, updated):
    profile = root / name
    (profile / "Network").mkdir(parents=True)
    (profile / "Preferences").write_text("{}")
    db = profile / "Network" / "Cookies"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE cookies (name TEXT, host_key TEXT, creation_utc INT, last_update_utc INT)")
        if updated:
            conn.execute("INSERT INTO cookies VALUES ('cf_clearance', '.linux.do', 1, ?)", (updated,))
    return db


@pytest.fixture
def chrome_root(tmp_path, monkeypatch):
    root = tmp_path / "Chrome"
    root.mkdir()
    monkeypatch.setattr(localcookies, "_platform", lambda: "darwin")
    monkeypatch.setattr(localcookies, "_ROOTS", {"darwin": {"chrome": str(root)}})
    monkeypatch.setattr(localcookies.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LINUXDO_UA", "UA/155")
    monkeypatch.delenv("LINUXDO_BROWSER", raising=False)
    return root


def test_reads_newest_profile(chrome_root, monkeypatch):
    make_profile(chrome_root, "Default", 100)
    newest = make_profile(chrome_root, "Profile 1", 900)
    make_profile(chrome_root, "Profile 2", 0)
    seen = {}

    def fake_cookies(url, cookie_file, browser):
        seen["file"] = cookie_file
        return {"cf_clearance": "from-newest", "_t": "never-used"}

    monkeypatch.setattr(pycookiecheat, "chrome_cookies", fake_cookies)
    assert localcookies.available()
    assert localcookies.read_clearance() == {"cf_clearance": "from-newest", "user_agent": "UA/155"}
    assert seen["file"] == str(newest)


def test_no_clearance_anywhere(chrome_root):
    make_profile(chrome_root, "Default", 0)
    assert localcookies.read_clearance() == {}


def test_default_user_agent_is_reduced_chrome(monkeypatch):
    monkeypatch.delenv("LINUXDO_UA", raising=False)
    monkeypatch.setattr(localcookies, "_platform", lambda: "darwin")
    monkeypatch.setattr(localcookies, "_major_version", lambda: "155")
    assert localcookies.user_agent() == (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
