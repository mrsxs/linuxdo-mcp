"""授权登录：走 Discourse User API Key 授权页拿一次性密码(OTP)，兑换成 MCP 独立的 _t 会话。

linux.do 的 allow_user_api_key_scopes 收窄过（不含 read/write），User API Key 本身读不了帖子；
能用的是 one_time_password：授权后回调里带一个加密的 OTP，拿它换一个全新的登录会话。

流程：
1. 本地生成 RSA 密钥对，打开 /user-api-key/new 授权页，点「授权」：
   - 装了回调小程序（handler.py）：在默认浏览器（日常已登录的账号）里授权
   - 没装：在专用浏览器里授权，需要时先过 Cloudflare 验证、登录 linux.do
2. 拿到服务端跳往 discourse://auth_redirect?payload=…&oneTimePassword=… 的回调
   （小程序写文件 / CDP 截获），私钥解密
3. GET /session/csrf + POST /session/otp/<otp> 兑换出新的 _t（独立会话，不和任何浏览器互顶）
4. 撤销刚生成的 User API Key（它只用来换 OTP，留着没有用处）
"""
import base64
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from . import browser, cf, client, handler, session, store

APPLICATION_NAME = "linuxdo-mcp"
SCOPES = "one_time_password"
AUTH_REDIRECT = "discourse://auth_redirect"
CLIENT_FILE = "client.json"
DEFAULT_TIMEOUT = 300
_DONE_HTML = ("<!doctype html><meta charset=utf-8><title>linuxdo-mcp</title>"
              "<p style='font:16px sans-serif;margin:3em'>授权完成，可以关闭此窗口。</p>")
_LOCK = threading.Lock()


def _client_id():
    """同一台机器固定用一个 client_id：Discourse 会在重新授权时清掉同 client 的旧 key。"""
    saved = store.read_json(CLIENT_FILE).get("client_id")
    if saved:
        return saved
    new = uuid.uuid4().hex
    store.write_json(CLIENT_FILE, {"client_id": new})
    return new


def new_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return key, pem


def build_auth_url(client_id, public_pem, nonce):
    query = urllib.parse.urlencode({
        "application_name": APPLICATION_NAME,
        "client_id": client_id,
        "scopes": SCOPES,
        "public_key": public_pem,
        "nonce": nonce,
        "auth_redirect": AUTH_REDIRECT,
    })
    return f"{client.BASE}/user-api-key/new?{query}"


def parse_callback(url):
    """从 discourse://auth_redirect?payload=…&oneTimePassword=… 取出两段 Base64 密文。"""
    if not url.startswith(AUTH_REDIRECT):
        raise RuntimeError("授权回调地址不对，已放弃。")
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    payload = qs.get("payload", [""])[0]
    otp = qs.get("oneTimePassword", [""])[0]
    if not payload or not otp:
        raise RuntimeError("授权回调缺少 payload 或 oneTimePassword。")
    return payload, otp


def decrypt(private_key, b64):
    """解密服务端 RSA-PKCS1 加密 + Base64（Ruby encode64 带换行）的数据。"""
    try:
        raw = base64.b64decode("".join(b64.split()), validate=True)
        return private_key.decrypt(raw, padding.PKCS1v15()).decode()
    except ValueError as e:  # 含 binascii.Error、UnicodeDecodeError、解密失败
        raise RuntimeError("授权回调解密失败（可能是残留的或别的应用的回调），请重新 login。") from e


def _decrypt_payload(private_key, b64):
    try:
        payload = json.loads(decrypt(private_key, b64))
    except ValueError as e:
        raise RuntimeError("授权回调内容不是预期格式，请重新 login。") from e
    if not isinstance(payload, dict):
        raise RuntimeError("授权回调内容不是预期格式，请重新 login。")
    return payload


def callback_from_paused(chrome, session_id, params):
    """在 Fetch.requestPaused（响应阶段）里找授权回调：302 的 Location 或 JSON 的 redirect_url。"""
    headers = {h["name"].lower(): h["value"] for h in params.get("responseHeaders") or []}
    location = headers.get("location", "")
    if location.startswith(AUTH_REDIRECT):
        return location
    if params.get("responseStatusCode") != 200 or "json" not in headers.get("content-type", ""):
        return None
    body = chrome.call("Fetch.getResponseBody", session_id=session_id, requestId=params["requestId"])
    text = base64.b64decode(body["body"]).decode() if body.get("base64Encoded") else body.get("body", "")
    try:
        redirect = json.loads(text).get("redirect_url") or ""
    except (ValueError, AttributeError):
        return None
    return redirect if redirect.startswith(AUTH_REDIRECT) else None


def _arm(chrome, session_id):
    """在授权页的会话上拦截 /user-api-key 的响应，并监听跳往外部协议的导航。"""
    chrome.call("Fetch.enable", session_id=session_id,
                patterns=[{"urlPattern": "*://linux.do/user-api-key*", "requestStage": "Response"}])
    chrome.call("Page.enable", session_id=session_id)


def _finish_paused(chrome, session_id, params, callback):
    request_id = params["requestId"]
    if callback and params.get("responseStatusCode") in (301, 302, 303):
        # 302 → discourse:// 时换成一个提示页，别让浏览器弹「打开外部应用」
        chrome.call("Fetch.fulfillRequest", session_id=session_id, requestId=request_id,
                    responseCode=200,
                    responseHeaders=[{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                    body=base64.b64encode(_DONE_HTML.encode()).decode())
    else:
        chrome.call("Fetch.continueRequest", session_id=session_id, requestId=request_id)


def wait_callback(chrome, target_id, timeout):
    """等用户在授权页点「授权」，返回截获的 discourse://auth_redirect 回调地址。"""
    _arm(chrome, chrome.attach(target_id))
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = chrome.next_event(timeout=1.0)
        if not ev:
            continue
        method, params, sid = ev.get("method"), ev.get("params") or {}, ev.get("sessionId")
        if method == "Fetch.requestPaused":
            url = callback_from_paused(chrome, sid, params)
            _finish_paused(chrome, sid, params, url)
            if url:
                return url
        elif method == "Page.frameRequestedNavigation" and params.get("url", "").startswith(AUTH_REDIRECT):
            return params["url"]
    raise RuntimeError(
        "等待授权超时：请在弹出的浏览器窗口里（需要时先过验证、登录 linux.do）点「授权」后重试。")


def exchange_otp(otp):
    """用一次性密码换新的 _t。CSRF 与 POST 必须在同一个 curl 会话里（共用 _forum_session）。"""
    if not re.fullmatch(r"[0-9a-f]+", otp):
        raise RuntimeError("一次性密码格式不对，已放弃。")
    http = client.new_session(with_login=False)
    xhr = {"X-Requested-With": "XMLHttpRequest"}
    csrf = client.request("GET", "/session/csrf", http=http, with_login=False, headers=xhr)
    token = (csrf.json() or {}).get("csrf") if csrf.status_code == 200 else None
    if not token:
        raise RuntimeError(f"兑换登录会话失败：拿不到 CSRF（HTTP {csrf.status_code}）。")
    # 一次性密码只能兑换一次：响应丢了也不能重放，直接失败让用户重新 login
    r = client.request("POST", f"/session/otp/{otp}", http=http, with_login=False, retry_network=False,
                       headers={**xhr, "X-CSRF-Token": token}, allow_redirects=False)
    cookie = r.cookies.get(session.COOKIE_NAME) or http.cookies.get(session.COOKIE_NAME)
    if not cookie:
        raise RuntimeError(f"兑换登录会话失败（HTTP {r.status_code}）：一次性密码可能已过期，请重新 login。")
    return f"{session.COOKIE_NAME}={cookie}"


def revoke_key(key, client_id):
    """撤销只用来换 OTP 的 User API Key；失败不影响登录结果，只提示一下。"""
    if not key:
        return
    try:
        r = client.request("POST", "/user-api-key/revoke", with_login=False,
                           headers={"User-Api-Key": key, "User-Api-Client-Id": client_id})
        problem = None if r.status_code == 200 else f"HTTP {r.status_code}"
    except Exception as e:
        problem = str(e)
    if problem:
        print(f"[linuxdo-mcp] 撤销临时 User API Key 失败（{problem}）；"
              "可在 linux.do 偏好设置→应用 里手动撤销。", file=sys.stderr)


def _callback_via_handler(auth_url, timeout):
    """日常浏览器授权：回调由 linuxdo-mcp-auth.app 接住写进缓存文件。"""
    if not cf.load():
        cf.refresh()  # 兑换 OTP 要过盾；先在专用浏览器把验证做掉，免得授权完再被打断
    store.remove(handler.CALLBACK_FILE)  # 清掉上次残留（浏览器偶尔会重复触发一次回调）
    started = time.time()
    handler.open_in_browser(auth_url)
    return handler.wait_callback(started, timeout)


def _callback_via_dedicated_browser(auth_url, timeout):
    """专用浏览器授权（没装回调小程序时的兜底）：CDP 直接截获回调。"""
    with browser.open_page(auth_url) as (chrome, target_id):
        callback = wait_callback(chrome, target_id, timeout)
        clearance = cf.harvest(chrome)  # 用户刚在这个窗口过了盾，顺手缓存给后续请求用
    if clearance:
        cf.save(clearance)
    return callback


def login(timeout=DEFAULT_TIMEOUT):
    """完整授权登录流程，成功后把新会话写入 session.json。

    装了 discourse:// 回调小程序（`linuxdo-mcp install-handler`）时在默认浏览器里授权，
    用的是你日常已登录的账号；否则退回专用浏览器（需要在其中登录 linux.do）。"""
    with _LOCK:
        private_key, public_pem = new_keypair()
        nonce, client_id = secrets.token_hex(16), _client_id()
        auth_url = build_auth_url(client_id, public_pem, nonce)
        if handler.installed():
            callback = _callback_via_handler(auth_url, timeout)
        else:
            callback = _callback_via_dedicated_browser(auth_url, timeout)
        payload_b64, otp_b64 = parse_callback(callback)
        payload = _decrypt_payload(private_key, payload_b64)
        try:
            if not secrets.compare_digest(str(payload.get("nonce", "")), nonce):
                raise RuntimeError("授权回调 nonce 不匹配，已放弃。")
            cookie = exchange_otp(decrypt(private_key, otp_b64))
        finally:  # 只要拿到了 key，成败都撤销，免得在用户「应用」列表里留下无用授权
            revoke_key(payload.get("key"), client_id)
        session.save(cookie)
