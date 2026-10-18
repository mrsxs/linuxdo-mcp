"""Cloudflare 过盾：拿真实浏览器铸出的 cf_clearance，连同该浏览器的 UA 一起缓存复用。

linux.do 对非浏览器客户端一律弹盾，光靠 curl_cffi 模拟 TLS 指纹已经过不去；
但真实浏览器过盾后铸出的 cf_clearance，配上同一个 UA，curl_cffi 就能正常访问。
cf_clearance 绑定 IP + UA，所以请求头里的 UA 必须用铸出它的那个浏览器的。

被拦时 refresh() 依次尝试：
1. 读日常浏览器里现成的 cf_clearance（localcookies.py，信任度高、最稳）
2. 在默认浏览器打开一次 linux.do 让它刷新 clearance，等写盘后再读
3. 都不行（Windows/Firefox 等读不了）时弹专用浏览器过盾；它铸的 clearance 信任度低，可能时好时坏
"""
import os
import sys
import threading
import time

from . import browser, handler, localcookies, store

BASE = "https://linux.do"
FILE = "cf.json"
_CHALLENGE_TITLES = ("Just a moment", "请稍候", "Attention Required")
_STABLE_SECS = 3
_DAILY_WAIT = 45  # Chrome 约 30 秒批量写一次 cookie 库，等这么久还没新值就别干等了
_LOCK = threading.Lock()


def _refresh_timeout():
    try:
        return int(os.environ.get("LINUXDO_CF_TIMEOUT", "180"))
    except ValueError:
        return 180


def load():
    """返回 {"cf_clearance", "user_agent", "ts"}；没有可用缓存时返回空 dict。"""
    data = store.read_json(FILE)
    return data if data.get("cf_clearance") and data.get("user_agent") else {}


def is_blocked(status, body, headers=None):
    """判断响应是不是 Cloudflare 的拦截/挑战页（而非 Discourse 自己的 403）。"""
    mitigated = (headers or {}).get("cf-mitigated", "")
    if mitigated == "challenge":
        return True
    if status not in (403, 429, 503):
        return False
    head = (body or "")[:3000]
    return "Just a moment" in head or "challenge-platform" in head or "cf-chl" in head


def harvest(chrome):
    """从专用浏览器取出 linux.do 的 cf_clearance 与 UA；还没有 clearance 时返回空 dict。"""
    value = next((c["value"] for c in chrome.cookies()
                  if c.get("name") == "cf_clearance" and c.get("domain", "").endswith("linux.do")), "")
    if not value:
        return {}
    return {"cf_clearance": value, "user_agent": chrome.user_agent()}


def save(data):
    store.write_json(FILE, {**data, "ts": time.time()})


def _on_site(chrome, target_id):
    title = next((p.get("title", "") for p in chrome.pages() if p.get("targetId") == target_id), "")
    return "LINUX DO" in title and not any(m in title for m in _CHALLENGE_TITLES)


def wait_for_clearance(chrome, target_id, timeout):
    """等页面越过挑战、真正进入 linux.do，且 cf_clearance 稳定几秒后返回。

    挑战进行中浏览器会先写一个临时 cf_clearance，那个值拿去用还是会被拦，
    所以必须等进站以后再取。
    """
    deadline = time.time() + timeout
    stable_since, last = time.time(), None
    while time.time() < deadline:
        data = harvest(chrome) if _on_site(chrome, target_id) else {}
        value = data.get("cf_clearance")
        if value and value == last:
            if time.time() - stable_since >= _STABLE_SECS:
                return data
        else:
            stable_since, last = time.time(), value
        time.sleep(1)
    raise RuntimeError(
        "等待 Cloudflare 验证超时：请在弹出的浏览器窗口里点「请验证您是真人」，"
        "进入 linux.do 后再重试。")


def _poll_local(stale, timeout):
    """等日常浏览器写盘一个与 stale 不同的 clearance（Chrome 约 30 秒批量写一次 cookie 库）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = localcookies.read_clearance()
        if data and data["cf_clearance"] != stale:
            return data
        time.sleep(3)
    return {}


def _from_daily_browser(stale):
    if not localcookies.available():
        return {}
    data = localcookies.read_clearance()
    if data and data["cf_clearance"] != stale:
        return data
    # 现成的没有或已被拒：让日常浏览器打开一次 linux.do 刷新 clearance
    handler.open_in_browser(BASE + "/")
    return _poll_local(stale, _DAILY_WAIT)


def _from_dedicated_browser():
    with browser.open_page(BASE + "/") as (chrome, target_id):
        return wait_for_clearance(chrome, target_id, _refresh_timeout())


def refresh(stale=""):
    """重新拿 clearance 并缓存。stale 是刚被拒的 clearance（之前没有则为空串）：
    如果排队期间别的调用已经刷新过（缓存值已变），就直接用新的，不再重复折腾。"""
    with _LOCK:
        current = load()
        if current and current["cf_clearance"] != (stale or ""):
            return current
        try:
            data = _from_daily_browser(stale)
        except RuntimeError as e:  # 比如钥匙串授权被拒
            print(f"[linuxdo-mcp] {e} 改用专用浏览器过盾。", file=sys.stderr)
            data = {}
        save(data or _from_dedicated_browser())
        return load()
