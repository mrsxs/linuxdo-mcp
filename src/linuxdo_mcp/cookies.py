"""登录 cookie 的获取与缓存。

优先级：
1. 环境变量 LINUXDO_COOKIE（显式指定，最高优先级）
2. 缓存文件 ~/.cache/linuxdo-mcp/cookie.json（未过期时直接用，避免频繁访问钥匙串）
3. 本机浏览器的 cookie 库（macOS 上 Chrome 系需解 Keychain，首次会弹一次授权框；
   Firefox 的 cookies.sqlite 未加密，任何平台都不需要授权）

浏览器里的 _t 是 Discourse 的滚动 cookie，只要平时还在用浏览器登录 linux.do，
这里读到的就一直是新鲜的，不必再手工导出粘贴。

相关环境变量：
    LINUXDO_BROWSER      chrome(默认)/chromium/brave/slack/firefox
    LINUXDO_COOKIE_TTL   缓存有效期秒数，默认 2592000（30 天，配合轮换自续期）
    LINUXDO_READ_BROWSER 置 1 才允许读浏览器（默认 0，避免与浏览器互顶）
    LINUXDO_CHROME_PROFILE 指定 Chrome 系 profile，可填显示名（如 "linuxdo"）
                         或目录名（如 "Profile 1"）；用专用 profile 与主浏览器互不干扰
"""
import json
import os
import pathlib
import sys
import time

COOKIE_NAME = "_t"
URL = "https://linux.do/"
CACHE = pathlib.Path(
    os.environ.get("LINUXDO_CACHE_DIR")
    or os.path.expanduser("~/.cache/linuxdo-mcp")
) / "cookie.json"


def _ttl():
    try:
        return int(os.environ.get("LINUXDO_COOKIE_TTL", "2592000"))
    except ValueError:
        return 2592000


def _normalize(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if "=" in raw else f"{COOKIE_NAME}={raw}"


def _read_cache():
    try:
        d = json.loads(CACHE.read_text())
    except Exception:
        return ""
    if time.time() - d.get("ts", 0) > _ttl():
        return ""
    return _normalize(d.get("cookie"))


def _write_cache(cookie):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"cookie": cookie, "ts": time.time()}))
        CACHE.chmod(0o600)
    except Exception:
        pass  # 缓存写不进去不影响主流程


def clear_cache():
    try:
        CACHE.unlink()
    except Exception:
        pass


def _list_chrome_profiles(root_abs):
    """返回 [(目录名, 显示名), ...]。显示名取自 Chrome 的 Local State/info_cache。"""
    names = {}
    ls = os.path.join(root_abs, "Local State")
    try:
        cache = json.load(open(ls)).get("profile", {}).get("info_cache", {})
        names = {d: (info or {}).get("name") for d, info in cache.items()}
    except Exception:
        pass
    out = []
    for entry in sorted(os.listdir(root_abs)):
        if os.path.isfile(os.path.join(root_abs, entry, "Preferences")):
            out.append((entry, names.get(entry)))
    return out


def _resolve_chrome_profile(root_abs, wanted):
    """把 LINUXDO_CHROME_PROFILE 解析成 profile 目录名：先按目录名，再按显示名匹配。"""
    profiles = _list_chrome_profiles(root_abs)
    dirs = {d for d, _ in profiles}
    if wanted in dirs:  # 直接给的就是目录名
        return wanted
    hits = [d for d, name in profiles if name and name == wanted]  # 按显示名
    if len(hits) == 1:
        return hits[0]
    listing = "、".join(
        f'「{name or "?"}」(目录 {d})' for d, name in profiles) or "（无）"
    if len(hits) > 1:
        raise RuntimeError(
            f"有多个 profile 显示名都是「{wanted}」：{hits}，请改用目录名。当前：{listing}")
    raise RuntimeError(
        f"找不到 profile「{wanted}」（可用显示名或目录名）。当前 Chrome profile：{listing}")


def _chrome_cookie_file():
    """若指定了 LINUXDO_CHROME_PROFILE（如 "Profile 1"），返回该 profile 的 Cookies 库路径。
    未指定则返回 None（走 pycookiecheat 默认的 Default profile）。"""
    profile = os.environ.get("LINUXDO_CHROME_PROFILE", "").strip()
    if not profile:
        return None
    name = os.environ.get("LINUXDO_BROWSER", "chrome").strip().lower()
    home = os.path.expanduser("~")
    roots = {
        "darwin": {
            "chrome": "Library/Application Support/Google/Chrome",
            "chromium": "Library/Application Support/Chromium",
            "brave": "Library/Application Support/BraveSoftware/Brave-Browser",
        },
        "linux": {
            "chrome": ".config/google-chrome",
            "chromium": ".config/chromium",
            "brave": ".config/BraveSoftware/Brave-Browser",
        },
    }
    plat = "darwin" if sys.platform == "darwin" else "linux"
    root = roots.get(plat, {}).get(name)
    if not root:
        raise RuntimeError(
            f"LINUXDO_CHROME_PROFILE 暂不支持 浏览器={name} 平台={sys.platform}。"
        )
    root_abs = os.path.join(home, root)
    profile_dir = _resolve_chrome_profile(root_abs, profile)  # 支持显示名或目录名
    # 新版 Chrome 的 cookie 库在 profile 下的 Network/Cookies，旧版直接在 profile/Cookies
    base = os.path.join(root_abs, profile_dir)
    for rel in ("Network/Cookies", "Cookies"):
        f = os.path.join(base, rel)
        if os.path.exists(f):
            return f
    raise RuntimeError(
        f"找不到 profile「{profile}」的 cookie 库（查过 {base}/Network/Cookies 与 /Cookies）；"
        "确认 LINUXDO_CHROME_PROFILE 是 profile 目录名（如 Default / Profile 1），"
        "且已在该 profile 里登录过 linux.do。"
    )


def _from_browser():
    """从本机浏览器 cookie 库读取 linux.do 的 _t。失败抛 RuntimeError。"""
    name = os.environ.get("LINUXDO_BROWSER", "chrome").strip().lower()
    if sys.platform.startswith("win") and name != "firefox":
        raise RuntimeError(
            "Windows 上无法自动解密 Chrome 系 cookie（pycookiecheat 只支持 macOS/Linux）。"
            "请改用 Firefox（设 LINUXDO_BROWSER=firefox）或手动设置 LINUXDO_COOKIE。"
        )
    try:
        import pycookiecheat
    except ImportError as e:
        raise RuntimeError(
            "缺少 pycookiecheat，无法自动读取浏览器 cookie；"
            "请安装（uv pip install pycookiecheat）或改用 LINUXDO_COOKIE 环境变量。"
        ) from e

    try:
        if name == "firefox":
            jar = pycookiecheat.firefox_cookies(URL)
        else:
            browser = getattr(pycookiecheat.BrowserType, name.upper(), None)
            if browser is None:
                raise RuntimeError(f"不支持的 LINUXDO_BROWSER={name}")
            jar = pycookiecheat.chrome_cookies(
                URL, browser=browser, cookie_file=_chrome_cookie_file())
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"读取 {name} 的 cookie 失败：{e}；"
            "若弹出钥匙串授权框请选「始终允许」，或改用 LINUXDO_COOKIE 环境变量。"
        ) from e

    token = jar.get(COOKIE_NAME)
    if not token:
        raise RuntimeError(
            f"在 {name} 中没找到 linux.do 的 {COOKIE_NAME} cookie，"
            "请先在该浏览器里登录 linux.do。"
        )
    return f"{COOKIE_NAME}={token}"


def _read_browser_enabled():
    return os.environ.get("LINUXDO_READ_BROWSER", "0").strip().lower() in (
        "1", "true", "yes", "on")


def get_cookie():
    """返回可直接用作 Cookie 头的字符串。

    来源优先级：
      1. 缓存文件（工具自维护，含轮换续期，最新）
      2. 环境变量 LINUXDO_COOKIE（首次 bootstrap，会写入缓存）
      3. 浏览器 cookie 库（仅当 LINUXDO_READ_BROWSER 开启）

    默认不读主浏览器：主浏览器与本工具共用同一 _t 会被 Discourse 互相顶下线。
    """
    cached = _read_cache()
    if cached:
        return cached
    env = _normalize(os.environ.get("LINUXDO_COOKIE"))
    if env:
        _write_cache(env)
        return env
    if _read_browser_enabled():
        cookie = _from_browser()
        _write_cache(cookie)
        return cookie
    raise RuntimeError(
        "未配置登录 cookie。请设置 LINUXDO_COOKIE=_t=... ；"
        "若确实要自动读浏览器，设 LINUXDO_READ_BROWSER=1——"
        "但读主浏览器会与它共用同一登录、可能互相顶下线，"
        "强烈建议改用隐身窗口/独立 profile 登录后取其独立 _t。"
    )


def absorb_rotation(response):
    """Discourse 会定期轮换 _t。若响应里带回新的 _t，就更新缓存，实现自续期。"""
    try:
        jar = getattr(response, "cookies", None)
        if not jar:
            return
        token = jar.get(COOKIE_NAME) if hasattr(jar, "get") else None
        if token:
            _write_cache(f"{COOKIE_NAME}={token}")
    except Exception:
        pass
