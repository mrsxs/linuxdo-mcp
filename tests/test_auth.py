import base64
import contextlib
import json
import urllib.parse

import pytest
from cryptography.hazmat.primitives.asymmetric import padding

from linuxdo_mcp import auth, cf, session


def ruby_encrypt(public_key, text):
    """模拟服务端：RSA-PKCS1 加密后 Base64.encode64（每 60 字符换行）。"""
    return base64.encodebytes(public_key.encrypt(text.encode(), padding.PKCS1v15())).decode()


def ruby_callback(payload_b64, otp_b64):
    """模拟服务端 CGI.escape 拼出的 discourse:// 回调。"""
    esc = lambda s: urllib.parse.quote_plus(s, safe="*-._")  # noqa: E731
    return f"{auth.AUTH_REDIRECT}?payload={esc(payload_b64)}&oneTimePassword={esc(otp_b64)}"


@pytest.fixture(scope="module")
def keypair():
    return auth.new_keypair()


def test_auth_url_carries_required_params(keypair):
    _, pem = keypair
    url = auth.build_auth_url("cid", pem, "n0nce")
    assert url.startswith("https://linux.do/user-api-key/new?")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert qs["scopes"] == ["one_time_password"]
    assert qs["auth_redirect"] == ["discourse://auth_redirect"]
    assert qs["public_key"] == [pem]
    assert qs["client_id"] == ["cid"] and qs["nonce"] == ["n0nce"]


def test_callback_roundtrip(keypair):
    key, _ = keypair
    payload = json.dumps({"key": "k", "nonce": "n", "push": False, "api": 4})
    url = ruby_callback(ruby_encrypt(key.public_key(), payload), ruby_encrypt(key.public_key(), "a1b2c3"))
    payload_b64, otp_b64 = auth.parse_callback(url)
    assert auth.decrypt(key, payload_b64) == payload
    assert auth.decrypt(key, otp_b64) == "a1b2c3"


@pytest.mark.parametrize("url", [
    "https://evil.example/?payload=a&oneTimePassword=b",
    "discourse://auth_redirect?payload=abc",
])
def test_parse_callback_rejects_bad_urls(url):
    with pytest.raises(RuntimeError):
        auth.parse_callback(url)


class PausedChrome:
    def __init__(self, body=None):
        self.body = body
        self.calls = []

    def call(self, method, session_id=None, **params):
        self.calls.append(method)
        return self.body


def test_callback_from_302_location():
    params = {"requestId": "r", "responseStatusCode": 302,
              "responseHeaders": [{"name": "Location", "value": "discourse://auth_redirect?payload=x"}]}
    assert auth.callback_from_paused(PausedChrome(), "s", params).startswith("discourse://")


def test_callback_from_json_body():
    body = json.dumps({"redirect_url": "discourse://auth_redirect?payload=y"})
    chrome = PausedChrome({"body": base64.b64encode(body.encode()).decode(), "base64Encoded": True})
    params = {"requestId": "r", "responseStatusCode": 200,
              "responseHeaders": [{"name": "Content-Type", "value": "application/json; charset=utf-8"}]}
    assert auth.callback_from_paused(chrome, "s", params) == "discourse://auth_redirect?payload=y"


def test_non_callback_responses_are_ignored():
    params = {"requestId": "r", "responseStatusCode": 200,
              "responseHeaders": [{"name": "Content-Type", "value": "text/html"}]}
    chrome = PausedChrome()
    assert auth.callback_from_paused(chrome, "s", params) is None
    assert chrome.calls == []  # 不读 HTML 页面的 body


def test_exchange_rejects_non_hex_otp():
    with pytest.raises(RuntimeError, match="格式"):
        auth.exchange_otp("../../admin")


@pytest.fixture
def login_harness(monkeypatch, keypair):
    """把浏览器、OTP 兑换、撤销都换成替身，只验证 login 的编排逻辑。"""
    key, pem = keypair
    seen = {}
    monkeypatch.setattr(auth, "new_keypair", lambda: (key, pem))
    monkeypatch.setattr(auth.secrets, "token_hex", lambda n: "nonce-1")

    @contextlib.contextmanager
    def open_page(url):
        seen["url"] = url
        yield object(), "T"

    monkeypatch.setattr(auth.handler, "installed", lambda: False)
    monkeypatch.setattr(auth.browser, "open_page", open_page)
    monkeypatch.setattr(auth.cf, "harvest", lambda chrome: {"cf_clearance": "c", "user_agent": "UA"})
    monkeypatch.setattr(auth, "exchange_otp", lambda otp: seen.setdefault("otp", otp) and "_t=fresh")
    monkeypatch.setattr(auth, "revoke_key", lambda k, cid: seen.setdefault("revoked", k))

    def with_callback(nonce):
        payload = json.dumps({"key": "apikey", "nonce": nonce, "push": False, "api": 4})
        url = ruby_callback(ruby_encrypt(key.public_key(), payload), ruby_encrypt(key.public_key(), "deadbeef"))
        monkeypatch.setattr(auth, "wait_callback", lambda chrome, target, timeout: url)
        monkeypatch.setattr(auth.handler, "wait_callback", lambda since, timeout: url)

    seen["with_callback"] = with_callback
    return seen


def test_login_saves_independent_session(login_harness):
    login_harness["with_callback"]("nonce-1")
    auth.login(timeout=5)
    assert session.get_cookie() == "_t=fresh"
    assert login_harness["otp"] == "deadbeef"
    assert login_harness["revoked"] == "apikey"
    assert cf.load()["cf_clearance"] == "c"
    assert "user-api-key/new" in login_harness["url"]


def test_login_rejects_nonce_mismatch(login_harness):
    login_harness["with_callback"]("someone-elses-nonce")
    with pytest.raises(RuntimeError, match="nonce"):
        auth.login(timeout=5)
    assert session.get_cookie() == ""
    assert "otp" not in login_harness
    assert login_harness["revoked"] == "apikey"  # 拿到的 key 照样撤销


def test_junk_callback_is_a_friendly_error(keypair):
    key, _ = keypair
    other, _ = auth.new_keypair()
    foreign = ruby_encrypt(other.public_key(), "{}")
    for junk in ("not base64!!", foreign):
        with pytest.raises(RuntimeError, match="解密失败"):
            auth.decrypt(key, junk)
    with pytest.raises(RuntimeError, match="格式"):
        auth._decrypt_payload(key, ruby_encrypt(key.public_key(), "[1, 2]"))
