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
    LINUXDO_COOKIE_TTL   缓存有效期秒数，默认 21600（6 小时）
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
        return int(os.environ.get("LINUXDO_COOKIE_TTL", "21600"))
    except ValueError:
        return 21600


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
            jar = pycookiecheat.chrome_cookies(URL, browser=browser)
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


def get_cookie(force=False):
    """返回可直接用作 Cookie 头的字符串。force=True 时跳过缓存重新读浏览器。"""
    env = _normalize(os.environ.get("LINUXDO_COOKIE"))
    if env:
        return env
    if not force:
        cached = _read_cache()
        if cached:
            return cached
    cookie = _from_browser()
    _write_cache(cookie)
    return cookie
