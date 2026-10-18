import pytest

from linuxdo_mcp import cf, client, session

from .conftest import FakeResponse

BLOCKED = FakeResponse(403, "<title>Just a moment...</title>")


class FakeHttp:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def wire(monkeypatch):
    """把 new_session 换成共享的 FakeHttp，记录 cf.refresh 调用。"""
    state = {"refreshes": 0}

    def setup(outcomes):
        http = FakeHttp(outcomes)
        monkeypatch.setattr(client, "new_session", lambda with_login=True: http)
        state["http"] = http
        return http

    def refresh(stale=None):
        state["refreshes"] += 1
        return {"cf_clearance": "new", "user_agent": "UA"}

    monkeypatch.setattr(cf, "refresh", refresh)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    state["setup"] = setup
    return state


def test_ok_passes_through(wire):
    wire["setup"]([FakeResponse(200, "{}")])
    assert client.request("GET", "/latest.json").status_code == 200
    assert wire["refreshes"] == 0
    assert wire["http"].calls[0][1] == "https://linux.do/latest.json"


def test_blocked_refreshes_once_then_retries(wire):
    wire["setup"]([BLOCKED, FakeResponse(200, "{}")])
    assert client.request("GET", "/latest.json").status_code == 200
    assert wire["refreshes"] == 1


def test_still_blocked_after_refresh_raises(wire):
    wire["setup"]([BLOCKED, BLOCKED, FakeResponse(200, "{}")])
    with pytest.raises(RuntimeError, match="Cloudflare"):
        client.request("GET", "/latest.json")
    assert wire["refreshes"] == 1


def test_block_after_network_errors_still_gets_its_retry(wire):
    wire["setup"]([OSError("reset"), OSError("reset"), BLOCKED, FakeResponse(200, "{}")])
    assert client.request("GET", "/x.json").status_code == 200
    assert wire["refreshes"] == 1


def test_non_replayable_request_fails_fast(wire):
    wire["setup"]([OSError("response lost"), FakeResponse(200, "{}")])
    with pytest.raises(RuntimeError, match="请求失败"):
        client.request("POST", "/session/otp/abc", retry_network=False)
    assert len(wire["http"].calls) == 1


def test_network_error_retries_without_browser(wire):
    wire["setup"]([OSError("SSL_connect: Connection closed abruptly"), FakeResponse(200, "{}")])
    assert client.request("GET", "/x.json").status_code == 200
    assert wire["refreshes"] == 0


def test_rotation_absorbed_only_with_login(wire):
    session.save("_t=old")
    wire["setup"]([FakeResponse(200, "{}", cookies={"_t": "rotated"})])
    client.request("GET", "/x.json", with_login=False)
    assert session.get_cookie() == "_t=old"
    wire["setup"]([FakeResponse(200, "{}", cookies={"_t": "rotated"})])
    client.request("GET", "/x.json")
    assert session.get_cookie() == "_t=rotated"
