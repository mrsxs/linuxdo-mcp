from linuxdo_mcp import auth, cf, session

from .test_auth import keypair, login_harness  # noqa: F401  复用 fixture


def test_login_via_handler_opens_default_browser(login_harness, monkeypatch):  # noqa: F811
    opened = []
    monkeypatch.setattr(auth.handler, "installed", lambda: True)
    monkeypatch.setattr(auth.handler, "open_in_browser", opened.append)
    monkeypatch.setattr(auth.cf, "refresh", lambda: cf.save({"cf_clearance": "pre", "user_agent": "UA"}))
    login_harness["with_callback"]("nonce-1")
    auth.login(timeout=5)
    assert opened and "user-api-key/new" in opened[0]
    assert "url" not in login_harness  # 没走专用浏览器授权
    assert cf.load()["cf_clearance"] == "pre"  # 没有 clearance 时先过盾
    assert session.get_cookie() == "_t=fresh"
