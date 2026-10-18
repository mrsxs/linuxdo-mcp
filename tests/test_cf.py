import contextlib

import pytest

from linuxdo_mcp import cf


@pytest.mark.parametrize("status,body,headers,expected", [
    (403, "<title>Just a moment...</title>", {}, True),
    (403, '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>', {}, True),
    (200, "<html>", {"cf-mitigated": "challenge"}, True),
    (403, '{"errors":["您没有权限查看所请求的资源。"],"error_type":"invalid_access"}', {}, False),
    (200, '<script src="/cdn-cgi/challenge-platform/scripts/precursor/main.js"></script>', {}, False),
    (200, '{"topic_list":{}}', {}, False),
])
def test_is_blocked(status, body, headers, expected):
    assert cf.is_blocked(status, body, headers) is expected


def test_load_requires_clearance_and_ua():
    cf.save({"cf_clearance": "c"})
    assert cf.load() == {}
    cf.save({"cf_clearance": "c", "user_agent": "UA"})
    assert cf.load()["user_agent"] == "UA"


class FakeChrome:
    """按调用次数依次给出页面标题与 cf_clearance，模拟「先挑战页、后进站」。"""

    def __init__(self, frames):
        self.frames = frames
        self.i = -1

    def pages(self):
        self.i = min(self.i + 1, len(self.frames) - 1)
        return [{"targetId": "T", "title": self.frames[self.i][0]}]

    def cookies(self):
        value = self.frames[self.i][1]
        return [{"name": "cf_clearance", "domain": ".linux.do", "value": value}] if value else []

    def user_agent(self):
        return "UA/155"


@pytest.fixture
def fake_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cf.time, "time", lambda: now[0])
    monkeypatch.setattr(cf.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def test_wait_ignores_interim_clearance_until_on_site(fake_clock):
    frames = [("Just a moment...", "interim")] * 5 + [("LINUX DO - 新的理想型社区", "final")] * 10
    data = cf.wait_for_clearance(FakeChrome(frames), "T", timeout=60)
    assert data == {"cf_clearance": "final", "user_agent": "UA/155"}


def test_wait_times_out_on_challenge(fake_clock):
    with pytest.raises(RuntimeError, match="超时"):
        cf.wait_for_clearance(FakeChrome([("Just a moment...", "interim")]), "T", timeout=10)


def test_refresh_dedupes_first_time_waiters(monkeypatch):
    """之前没有 clearance 时排队的调用（stale 为空）也不应再刷一遍。"""
    cf.save({"cf_clearance": "fresh", "user_agent": "UA"})
    monkeypatch.setattr(cf.browser, "open_page", lambda url: pytest.fail("不该再弹浏览器"))
    assert cf.refresh(stale="")["cf_clearance"] == "fresh"


def test_refresh_skips_browser_when_already_refreshed(monkeypatch):
    cf.save({"cf_clearance": "fresh", "user_agent": "UA"})

    def boom(url):
        raise AssertionError("不该再弹浏览器")

    monkeypatch.setattr(cf.browser, "open_page", boom)
    assert cf.refresh(stale="old")["cf_clearance"] == "fresh"


@pytest.fixture
def no_dedicated(monkeypatch):
    def boom(url):
        raise AssertionError("不该弹专用浏览器")

    monkeypatch.setattr(cf.browser, "open_page", boom)


def test_refresh_prefers_daily_browser(monkeypatch, no_dedicated):
    monkeypatch.setattr(cf.localcookies, "available", lambda: True)
    monkeypatch.setattr(cf.localcookies, "read_clearance", lambda: {"cf_clearance": "daily", "user_agent": "UA"})
    assert cf.refresh(stale="old")["cf_clearance"] == "daily"


def test_refresh_reopens_site_when_daily_value_is_stale(monkeypatch, fake_clock, no_dedicated):
    values = iter(["old", "old", "renewed"])
    opened = []
    monkeypatch.setattr(cf.localcookies, "available", lambda: True)
    monkeypatch.setattr(cf.localcookies, "read_clearance",
                        lambda: {"cf_clearance": next(values), "user_agent": "UA"})
    monkeypatch.setattr(cf.handler, "open_in_browser", opened.append)
    assert cf.refresh(stale="old")["cf_clearance"] == "renewed"
    assert opened == ["https://linux.do/"]


def test_refresh_falls_back_to_dedicated_browser(monkeypatch, fake_clock):
    frames = [("LINUX DO", "minted")] * 10

    @contextlib.contextmanager
    def open_page(url):
        assert url == "https://linux.do/"
        yield FakeChrome(frames), "T"

    def keychain_denied():
        raise RuntimeError("读取 chrome 的 cookie 失败：钥匙串拒绝")

    monkeypatch.setattr(cf.localcookies, "available", lambda: True)
    monkeypatch.setattr(cf.localcookies, "read_clearance", keychain_denied)
    monkeypatch.setattr(cf.browser, "open_page", open_page)
    assert cf.refresh()["cf_clearance"] == "minted"
    assert cf.load()["cf_clearance"] == "minted"
