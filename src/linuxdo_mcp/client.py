"""linux.do 请求：curl_cffi 模拟 Chrome TLS 指纹 + 浏览器铸出的 cf_clearance/UA + 会话 cookie。

被 Cloudflare 拦截时自动调用 cf.refresh() 换 clearance，然后保证再重试一次。
"""
import os
import time

from curl_cffi import requests as creq

from . import cf, session

BASE = "https://linux.do"
IMPERSONATE = os.environ.get("LINUXDO_IMPERSONATE", "chrome")
TIMEOUT = 30
_NETWORK_ATTEMPTS = 3


def new_session(with_login=True):
    """建一个带好 UA 与 cookie（cf_clearance、可选 _t）的 curl_cffi 会话。"""
    clearance = cf.load()
    s = creq.Session(impersonate=IMPERSONATE, timeout=TIMEOUT)
    s.headers.update({"Accept": "application/json"})
    if clearance:
        s.headers.update({"User-Agent": clearance["user_agent"]})
        s.cookies.set("cf_clearance", clearance["cf_clearance"], domain=".linux.do")
    if with_login:
        name, _, value = session.get_cookie().partition("=")
        if value:
            s.cookies.set(name, value, domain="linux.do")
    return s


def request(method, path, *, with_login=True, http=None, retry_network=True, **kwargs):
    """发请求并处理 Cloudflare：网络异常退避重试，被盾拦截则换 clearance 后再试一次。

    http 传入已有会话时沿用它的 cookie（比如 CSRF 与 POST 必须在同一会话里）。
    retry_network=False 用于不可重放的请求（如兑换一次性密码）：网络异常直接失败。
    """
    url = path if path.startswith("http") else BASE + path
    refreshed, failures = False, 0
    while True:
        s = http or new_session(with_login)
        try:
            r = s.request(method, url, **kwargs)
        except Exception as e:  # TLS 被掐断、超时等
            failures += 1
            if not retry_network or failures >= _NETWORK_ATTEMPTS:
                raise RuntimeError(f"请求失败：{e}") from e
            time.sleep(0.8 * failures)
            continue
        if not cf.is_blocked(r.status_code, r.text, r.headers):
            if with_login:
                session.absorb_rotation(r)
            return r
        if refreshed:
            raise RuntimeError(
                "被 Cloudflare 拦截（已换 clearance 重试）。请在日常浏览器打开一次 linux.do，"
                "如出现「请验证您是真人」点一下后再试；也可设 LINUXDO_IMPERSONATE=chrome146 换指纹。")
        fresh = cf.refresh(stale=cf.load().get("cf_clearance", ""))
        refreshed = True
        if http is not None:  # 沿用的会话也换上新 clearance
            http.headers.update({"User-Agent": fresh["user_agent"]})
            http.cookies.set("cf_clearance", fresh["cf_clearance"], domain=".linux.do")
