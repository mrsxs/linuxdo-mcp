"""从日常 Chrome 系浏览器读 linux.do 的 cf_clearance —— 只读这一个 cookie，不碰登录态 _t。

日常浏览器有长期的浏览记录，Cloudflare 给它铸的 clearance 信任度高，curl_cffi 拿去用很稳；
新建的专用浏览器铸出的 clearance 只是勉强过线，时好时坏，所以优先用这里的。

会扫描该浏览器所有 profile，取最近更新的那一份；UA 按浏览器版本号拼成它实际发送的样子
（Chrome 的 UA 已精简为 Chrome/<主版本>.0.0.0），也可用 LINUXDO_UA 直接指定。

相关环境变量：
    LINUXDO_BROWSER  chrome(默认)/chromium/brave
    LINUXDO_UA       覆盖自动拼出的 User-Agent
macOS 首次读取会弹一次钥匙串授权（访问「Chrome Safe Storage」），选「始终允许」。
"""
import contextlib
import os
import pathlib
import plistlib
import re
import sqlite3
import subprocess
import sys
import urllib.parse

URL = "https://linux.do/"
_ROOTS = {
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
_MAC_PLISTS = {
    "chrome": "/Applications/Google Chrome.app/Contents/Info.plist",
    "chromium": "/Applications/Chromium.app/Contents/Info.plist",
    "brave": "/Applications/Brave Browser.app/Contents/Info.plist",
}
_LINUX_BINS = {"chrome": "google-chrome", "chromium": "chromium", "brave": "brave-browser"}


def _browser():
    return os.environ.get("LINUXDO_BROWSER", "chrome").strip().lower()


def _platform():
    return "darwin" if sys.platform == "darwin" else "linux"


def available():
    """当前平台/浏览器是否支持自动读取（Windows、Firefox 等走专用浏览器兜底）。"""
    if not sys.platform.startswith(("darwin", "linux")):
        return False
    root = _ROOTS[_platform()].get(_browser())
    return bool(root) and (pathlib.Path.home() / root).is_dir()


def _cookie_dbs():
    root = pathlib.Path.home() / _ROOTS[_platform()][_browser()]
    for profile in sorted(root.iterdir()):
        if profile.name in ("System Profile", "Guest Profile") or not (profile / "Preferences").exists():
            continue
        for rel in ("Network/Cookies", "Cookies"):
            if (profile / rel).exists():
                yield profile / rel
                break


def _clearance_updated_at(db):
    """不解密，只看元数据：这个库里 linux.do 的 cf_clearance 最后更新时间（微秒）；没有则 0。"""
    uri = f"file:{urllib.parse.quote(str(db))}?mode=ro&immutable=1"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            for col in ("last_update_utc", "creation_utc"):
                try:
                    row = conn.execute(
                        f"SELECT MAX({col}) FROM cookies WHERE name='cf_clearance' AND host_key LIKE '%linux.do'"
                    ).fetchone()
                    return row[0] or 0
                except sqlite3.OperationalError:
                    continue
    except sqlite3.Error:
        return 0
    return 0


def _major_version():
    name = _browser()
    if _platform() == "darwin":
        try:
            info = plistlib.loads(pathlib.Path(_MAC_PLISTS[name]).read_bytes())
            return info["CFBundleShortVersionString"].split(".")[0]
        except (OSError, KeyError, ValueError):
            return ""
    try:
        out = subprocess.run([_LINUX_BINS[name], "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"(\d+)\.\d+\.\d+", out)
    return m.group(1) if m else ""


def user_agent():
    explicit = os.environ.get("LINUXDO_UA", "").strip()
    if explicit:
        return explicit
    major = _major_version()
    if not major:
        return ""
    system = "Macintosh; Intel Mac OS X 10_15_7" if _platform() == "darwin" else "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def read_clearance():
    """返回 {"cf_clearance", "user_agent"}；读不到时返回空 dict（失败原因抛 RuntimeError）。"""
    try:
        import pycookiecheat
    except ImportError as e:
        raise RuntimeError("缺少 pycookiecheat，无法读取浏览器 cookie。") from e
    dbs = [(db, _clearance_updated_at(db)) for db in _cookie_dbs()]
    dbs = [d for d in dbs if d[1]]
    if not dbs:
        return {}
    newest = max(dbs, key=lambda d: d[1])[0]
    browser = getattr(pycookiecheat.BrowserType, _browser().upper())
    try:
        jar = pycookiecheat.chrome_cookies(URL, cookie_file=str(newest), browser=browser)
    except Exception as e:
        raise RuntimeError(
            f"读取 {_browser()} 的 cookie 失败：{e}；若弹出钥匙串授权框请选「始终允许」。") from e
    if not isinstance(jar, dict):  # as_cookies=True 时才会是 list，这里不会走到
        return {}
    value, ua = jar.get("cf_clearance"), user_agent()
    return {"cf_clearance": value, "user_agent": ua} if value and ua else {}
