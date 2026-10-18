"""linux.do 请求：curl_cffi 模拟 Chrome TLS 指纹 + 浏览器铸出的 cf_clearance/UA + 会话 cookie。

请求头按「linux.do 页面里发出的 XHR」来配：sec-ch-ua 与 UA 同一版本、Sec-Fetch 为 cors/empty、
带 Referer。被 Cloudflare 拦截时自动调用 cf.refresh() 换 clearance，然后保证再重试一次。

Cloudflare 限流（429 + 挑战页）按「IP + _cfuvid cookie」分访客计数：实测每个访客约 4 分钟里
发到 120～150 条就触发（每分钟 60 条约 2 分钟、40 条约 4 分钟触发，35 条连跑 10 分钟不触发），
触发后约 4 分钟才恢复，期间换 clearance 也没用。所以限速分两层（见 _pace）：
- 瞬时：两次请求至少隔 MIN_INTERVAL 秒，小批量请求基本不用等；
- 总量：任意 WINDOW_SECONDS 秒内最多 WINDOW_LIMIT 条，额度用满就等最早那条移出窗口，
  要等太久时直接报错，不让工具卡住几分钟。
真被限流时直接报错，并在 RATE_LIMIT_COOLDOWN 秒内不再发请求。

不带 _cfuvid 的请求全挤在同一个 IP 桶里，几条就被拦。所以会话按登录态复用（顺带省掉每次的
TLS 握手，也少碰到握手被掐断），重建会话时带上记下来的 _cfuvid/__cf_bm。
"""
import collections
import math
import os
import re
import threading
import time

from curl_cffi import requests as creq

from . import cf, session

BASE = "https://linux.do"
IMPERSONATE = os.environ.get("LINUXDO_IMPERSONATE", "chrome")
TIMEOUT = 30
MIN_INTERVAL = float(os.environ.get("LINUXDO_MIN_INTERVAL", "0.5"))
WINDOW_LIMIT = int(os.environ.get("LINUXDO_WINDOW_LIMIT", "110"))  # 实测最低触发点约 120，留余量
WINDOW_SECONDS = float(os.environ.get("LINUXDO_WINDOW_SECONDS", "300"))
MAX_WAIT = 30  # 额度用满时最多原地等这么久，再久就报错
RATE_LIMIT_COOLDOWN = float(os.environ.get("LINUXDO_RATE_LIMIT_COOLDOWN", "240"))
_NETWORK_ATTEMPTS = 3
_MAX_RETRY_AFTER = 20
_GATE = threading.Lock()
_last_sent = 0.0
_sent = collections.deque()  # 窗口内每次发出请求的时刻（monotonic）
_limited_until = 0.0  # 被 Cloudflare 限流后，这个时刻（monotonic）之前不再发请求
_CF_VISITOR_COOKIES = ("_cfuvid", "__cf_bm")
_cf_jar = {}  # Cloudflare 发的访客 cookie，跨会话保留（进程内）
_sessions = {}  # 是否登录 -> (会话依据的 clearance/UA/_t, 复用的会话)

# 页面里 fetch/XHR 的请求头；impersonate 默认带的是「地址栏导航」那套，换掉
_XHR_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Discourse-Present": "true",
    "Referer": BASE + "/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-User": None,  # None = 去掉 impersonate 自带的这个头
    "Upgrade-Insecure-Requests": None,
}
_GREASE_CHARS = " (:-./);=?_"
_GREASE_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def sec_ch_ua(major):
    """按 Chromium 的 GenerateBrandVersionList 算出该大版本真实发送的 sec-ch-ua。"""
    grease = (f'"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand";'
              f'v="{_GREASE_VERSIONS[major % 3]}"')
    brands = ["", "", ""]
    order = _BRAND_ORDERS[major % 6]
    brands[order[0]] = grease
    brands[order[1]] = f'"Chromium";v="{major}"'
    brands[order[2]] = f'"Google Chrome";v="{major}"'
    return ", ".join(brands)


def client_hints(user_agent):
    """UA 对应的 sec-ch-ua 三件套；不是 Chrome UA 时返回空（沿用 impersonate 默认值）。"""
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    if not m:
        return {}
    platform = "macOS" if "Macintosh" in user_agent else "Windows" if "Windows" in user_agent else "Linux"
    return {"sec-ch-ua": sec_ch_ua(int(m.group(1))), "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": f'"{platform}"'}


def resolve_impersonate(user_agent):
    """按 clearance 的 UA 选 curl_cffi 的 impersonate 目标，让 TLS 指纹与 UA 同一 Chrome 版本。

    显式设了 LINUXDO_IMPERSONATE 就原样用它（覆盖）；否则从 UA 取 Chrome 主版本，库里
    有 chrome<主版本> 就用它，没有（浏览器比 curl_cffi 还新）就回退到 ≤ 主版本里最接近的。
    非 Chrome UA 时沿用默认值 IMPERSONATE。
    """
    explicit = os.environ.get("LINUXDO_IMPERSONATE", "").strip()
    if explicit:
        return explicit
    m = re.search(r"Chrome/(\d+)", user_agent or "")
    if not m:
        return IMPERSONATE
    target = f"chrome{m.group(1)}"
    if hasattr(creq.BrowserType, target):
        return target
    versions = sorted(int(k[6:]) for k in dir(creq.BrowserType)
                      if k.startswith("chrome") and k[6:].isdigit())
    older = [v for v in versions if v <= int(m.group(1))]
    return f"chrome{older[-1]}" if older else IMPERSONATE


def _pace():
    """两层限速（含重试）：瞬时至少隔 MIN_INTERVAL 秒；任意 WINDOW_SECONDS 秒内最多 WINDOW_LIMIT 条。

    额度用满就等最早那条移出窗口；要等超过 MAX_WAIT 秒时直接报错，告诉调用方多久后恢复。
    """
    global _last_sent
    with _GATE:
        now = time.monotonic()
        while _sent and _sent[0] <= now - WINDOW_SECONDS:
            _sent.popleft()
        wait = _last_sent + MIN_INTERVAL - now
        if len(_sent) >= WINDOW_LIMIT:
            wait = max(wait, _sent[0] + WINDOW_SECONDS - now)
        if wait > MAX_WAIT:
            raise RuntimeError(
                f"最近 {WINDOW_SECONDS / 60:g} 分钟已发 {len(_sent)} 条请求，再多会被 Cloudflare 限流；"
                f"约 {math.ceil(wait)} 秒后恢复额度，可以先看已经取到的内容。")
        if wait > 0:
            time.sleep(wait)
        _last_sent = time.monotonic()
        _sent.append(_last_sent)


def _absorb_cf_cookies(response):
    """记下响应里 Cloudflare 新发的访客 cookie，后续会话带上，免得落进不带 cookie 的共用 IP 桶。"""
    jar = getattr(response, "cookies", None)
    if jar is None or not hasattr(jar, "get"):
        return
    for name in _CF_VISITOR_COOKIES:
        if value := jar.get(name):
            _cf_jar[name] = value


def _rate_limited_error(seconds_left):
    return RuntimeError(
        f"被 Cloudflare 限流（请求太密），约 {math.ceil(seconds_left)} 秒后恢复，期间请不要继续请求；"
        "批量读帖请分批，或调低 LINUXDO_WINDOW_LIMIT。")


def _retry_after(r):
    """Discourse 自己的 429（非 Cloudflare 挑战）给的等待秒数；没有或太久返回 None。"""
    try:
        seconds = int(r.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= _MAX_RETRY_AFTER else None


def new_session(with_login=True):
    """建一个带好 UA、XHR 请求头与 cookie（cf_clearance、可选 _t）的 curl_cffi 会话。"""
    clearance = cf.load()
    s = creq.Session(impersonate=resolve_impersonate(clearance.get("user_agent")), timeout=TIMEOUT)
    s.headers.update(_XHR_HEADERS)
    if clearance:
        s.headers.update({"User-Agent": clearance["user_agent"], **client_hints(clearance["user_agent"])})
        s.cookies.set("cf_clearance", clearance["cf_clearance"], domain=".linux.do")
    for name, value in _cf_jar.items():
        s.cookies.set(name, value, domain=".linux.do")
    if with_login:
        name, _, value = session.get_cookie().partition("=")
        if value:
            s.cookies.set(name, value, domain="linux.do")
            s.headers.update({"Discourse-Logged-In": "true"})
    return s


def _session(with_login):
    """按登录态复用会话（连接和 cookie 都留着）；clearance、UA 或 _t 变了就换一个新的。"""
    clearance = cf.load()
    basis = (clearance.get("cf_clearance"), clearance.get("user_agent"),
             session.get_cookie() if with_login else "")
    cached = _sessions.get(with_login)
    if cached and cached[0] == basis:
        return cached[1]
    s = new_session(with_login)
    _sessions[with_login] = (basis, s)
    return s


def request(method, path, *, with_login=True, http=None, retry_network=True, **kwargs):
    """发请求并处理 Cloudflare：网络异常退避重试，被盾拦截则换 clearance 后再试一次。

    http 传入已有会话时沿用它的 cookie（比如 CSRF 与 POST 必须在同一会话里）。
    retry_network=False 用于不可重放的请求（如兑换一次性密码）：网络异常直接失败。
    """
    global _limited_until
    url = path if path.startswith("http") else BASE + path
    if (left := _limited_until - time.monotonic()) > 0:
        raise _rate_limited_error(left)
    refreshed, waited, failures = False, False, 0
    while True:
        s = http or _session(with_login)
        _pace()
        try:
            r = s.request(method, url, **kwargs)
        except Exception as e:  # TLS 被掐断、超时等
            failures += 1
            if not retry_network or failures >= _NETWORK_ATTEMPTS:
                raise RuntimeError(f"请求失败：{e}") from e
            time.sleep(0.8 * failures)
            continue
        _absorb_cf_cookies(r)
        if cf.is_rate_limited(r.status_code, r.text, r.headers):
            _limited_until = time.monotonic() + RATE_LIMIT_COOLDOWN
            raise _rate_limited_error(RATE_LIMIT_COOLDOWN)
        if not cf.is_blocked(r.status_code, r.text, r.headers):
            delay = _retry_after(r) if r.status_code == 429 and not waited and retry_network else None
            if delay is not None:  # Discourse 限流：按 Retry-After 等一次再试
                waited = True
                time.sleep(delay)
                continue
            if with_login:
                session.absorb_rotation(r)
            return r
        if refreshed:
            raise RuntimeError(
                "被 Cloudflare 拦截（已换 clearance 重试）。请在日常浏览器打开一次 linux.do，"
                "如出现「请验证您是真人」点一下后再试；也可设 LINUXDO_IMPERSONATE=chrome146 换指纹。")
        fresh = cf.refresh(stale=cf.load().get("cf_clearance", ""))
        refreshed = True
        if http is not None:  # 沿用的会话也换上新 clearance，sec-ch-ua 跟着新 UA 走
            http.headers.update({"User-Agent": fresh["user_agent"], **client_hints(fresh["user_agent"])})
            http.cookies.set("cf_clearance", fresh["cf_clearance"], domain=".linux.do")
