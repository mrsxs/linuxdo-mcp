"""专用 Chrome：独立 profile + CDP 远程调试，用来过 Cloudflare 验证、完成授权登录。

profile 在 <缓存目录>/chrome-profile，与日常用的浏览器完全隔离；里面的验证记录和
linux.do 登录态会保留，之后再过盾通常能自动通过、再授权也不用重新登录。

浏览器路径自动探测 Chrome / Chromium / Edge / Brave，也可用 LINUXDO_CHROME 指定可执行文件。
"""
import collections
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from websockets.sync.client import connect

from . import store

PROFILE = "chrome-profile"
_MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_LINUX_BINS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
               "microsoft-edge", "brave-browser")
_LAUNCH_TIMEOUT = 30


def chrome_path():
    explicit = os.environ.get("LINUXDO_CHROME", "").strip()
    if explicit:
        return explicit
    if sys.platform == "darwin":
        found = [p for p in _MAC_APPS if os.path.exists(p)]
    else:
        found = [p for p in (shutil.which(b) for b in _LINUX_BINS) if p]
    if not found:
        raise RuntimeError(
            "找不到 Chrome/Chromium/Edge/Brave；请安装其一，"
            "或设置 LINUXDO_CHROME=浏览器可执行文件路径。")
    return found[0]


class Chrome:
    """一个 CDP 浏览器级连接。命令走 call()，期间收到的事件缓存起来给 next_event() 取。"""

    def __init__(self, ws, proc=None):
        self._ws = ws
        self._proc = proc
        self._seq = 0
        self._events = collections.deque()

    def call(self, method, session_id=None, timeout=30, **params):
        self._seq += 1
        seq = self._seq
        msg = {"id": seq, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} 超时")
            m = json.loads(self._ws.recv(timeout=remaining))
            if m.get("id") == seq:
                if "error" in m:
                    raise RuntimeError(f"CDP {method} 失败：{m['error'].get('message')}")
                return m.get("result", {})
            self._events.append(m)

    def next_event(self, timeout):
        """取下一个 CDP 事件；timeout 秒内没有则返回 None。"""
        if self._events:
            return self._events.popleft()
        try:
            return json.loads(self._ws.recv(timeout=timeout))
        except TimeoutError:
            return None

    def user_agent(self):
        return self.call("Browser.getVersion")["userAgent"]

    def cookies(self):
        return self.call("Storage.getCookies")["cookies"]

    def pages(self):
        return [t for t in self.call("Target.getTargets")["targetInfos"] if t.get("type") == "page"]

    def attach(self, target_id):
        return self.call("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]

    def close(self, target_id=None):
        """关闭浏览器。若是连上的已有实例（不是本次启动的），只关自己开的标签页，
        免得把正在进行的另一个流程（比如授权登录）的窗口一起关掉。"""
        with contextlib.suppress(Exception):
            if self._proc is None and target_id:
                self.call("Target.closeTarget", targetId=target_id, timeout=5)
            else:
                self.call("Browser.close", timeout=5)
        with contextlib.suppress(Exception):
            self._ws.close()
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _profile_dir():
    path = store.CACHE_DIR / PROFILE
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ws_url_from(port_file):
    """Chrome 开启 --remote-debugging-port=0 后把实际端口与 ws 路径写进 DevToolsActivePort。"""
    lines = port_file.read_text().split()
    if len(lines) < 2:
        raise ValueError("DevToolsActivePort 内容不完整")
    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"


def _open_tab_in_existing(port_file, url):
    """专用浏览器已在运行（比如上次的窗口没关）时连上它开新标签，避免同一 profile 起第二个实例。"""
    if not port_file.exists():
        return None
    try:
        ws = connect(_ws_url_from(port_file), max_size=None, open_timeout=3)
    except Exception:
        return None
    chrome = Chrome(ws)
    try:
        return chrome, chrome.call("Target.createTarget", url=url)["targetId"]
    except Exception:
        with contextlib.suppress(Exception):  # 不是我们启动的实例，只断开连接，不关浏览器
            ws.close()
        return None


def _initial_target(chrome, url):
    pages = chrome.pages()
    target = next((p for p in pages if p.get("url", "").startswith(url[:40])), None)
    target = target or (pages[0] if pages else None)
    if target is None:
        return chrome.call("Target.createTarget", url=url)["targetId"]
    return target["targetId"]


def _launch(url):
    profile = _profile_dir()
    port_file = profile / "DevToolsActivePort"
    existing = _open_tab_in_existing(port_file, url)
    if existing is not None:
        return existing

    port_file.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [chrome_path(), f"--user-data-dir={profile}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", "--window-size=1000,800", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    chrome = None
    try:
        deadline = time.time() + _LAUNCH_TIMEOUT
        while chrome is None:
            if proc.poll() is not None:
                raise RuntimeError(f"专用浏览器启动后立即退出（code={proc.returncode}）。")
            if time.time() > deadline:
                raise RuntimeError("专用浏览器启动超时（未拿到远程调试端口）。")
            try:
                chrome = Chrome(connect(_ws_url_from(port_file), max_size=None, open_timeout=3), proc)
            except (OSError, ValueError):
                time.sleep(0.3)
        return chrome, _initial_target(chrome, url)
    except BaseException:
        if chrome is not None:
            chrome.close()
        if proc.poll() is None:
            proc.kill()
        raise


# 专用 profile 同一时间只给一个流程用（授权登录与过盾兜底可能在不同线程里同时触发）
_PROFILE_LOCK = threading.RLock()


@contextlib.contextmanager
def open_page(url):
    """在专用浏览器里打开 url，产出 (Chrome, targetId)；退出时关闭浏览器。"""
    with _PROFILE_LOCK:
        chrome, target_id = _launch(url)
        try:
            yield chrome, target_id
        finally:
            chrome.close(target_id)
