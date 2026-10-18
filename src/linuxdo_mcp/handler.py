"""macOS 上接收 discourse:// 授权回调的小程序，让授权在你日常（已登录）的浏览器里完成。

linux.do 只允许 discourse://auth_redirect 作回调。装上 linuxdo-mcp-auth.app 并设为
discourse: 的默认处理程序后：login 在默认浏览器打开授权页 → 点「授权」→ 浏览器询问是否
打开 linuxdo-mcp-auth → 它把回调地址写进 <缓存目录>/callback.url（600 权限）→ login 读取。
回调里的 payload / OTP 都是用本次 login 临时生成的公钥加密的，只有发起 login 的进程能解开。

安装会记下原来的默认处理程序（比如 FluxDO），uninstall 时还原。
"""
import ctypes
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

from . import store

SCHEME = "discourse"
BUNDLE_ID = "io.github.mrsxs.linuxdo-mcp-auth"
APP_PATH = pathlib.Path.home() / "Applications" / "linuxdo-mcp-auth.app"
CALLBACK_FILE = "callback.url"
STATE_FILE = "handler.json"
_LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
               "LaunchServices.framework/Support/lsregister")
_UTF8 = 0x08000100

# 先写临时文件再 mv：读的一方要么看不到文件，要么看到完整内容
_APPLESCRIPT = '''on open location theURL
	do shell script "umask 077; mkdir -p " & quoted form of "{dir}" & "; printf '%s' " & quoted form of theURL & " > " & quoted form of "{file}.part" & " && mv -f " & quoted form of "{file}.part" & " " & quoted form of "{file}"
end open location
'''


def _require_macos():
    if sys.platform != "darwin":
        raise RuntimeError("discourse:// 回调小程序目前只支持 macOS。")


def _cf_libs():
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ls = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    ls.LSCopyDefaultHandlerForURLScheme.restype = ctypes.c_void_p
    ls.LSCopyDefaultHandlerForURLScheme.argtypes = [ctypes.c_void_p]
    ls.LSSetDefaultHandlerForURLScheme.restype = ctypes.c_int32
    ls.LSSetDefaultHandlerForURLScheme.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return cf, ls


def default_handler():
    """当前 discourse: 的默认处理程序 bundle id；没有则返回空串。"""
    _require_macos()
    cf, ls = _cf_libs()
    scheme = cf.CFStringCreateWithCString(None, SCHEME.encode(), _UTF8)
    try:
        ref = ls.LSCopyDefaultHandlerForURLScheme(scheme)
        if not ref:
            return ""
        buf = ctypes.create_string_buffer(512)
        ok = cf.CFStringGetCString(ref, buf, len(buf), _UTF8)
        cf.CFRelease(ref)
        return buf.value.decode() if ok else ""
    finally:
        cf.CFRelease(scheme)


def _set_default_handler(bundle_id):
    cf, ls = _cf_libs()
    scheme = cf.CFStringCreateWithCString(None, SCHEME.encode(), _UTF8)
    bundle = cf.CFStringCreateWithCString(None, bundle_id.encode(), _UTF8)
    try:
        status = ls.LSSetDefaultHandlerForURLScheme(scheme, bundle)
    finally:
        cf.CFRelease(scheme)
        cf.CFRelease(bundle)
    if status != 0:
        raise RuntimeError(f"设置 {SCHEME}: 默认处理程序失败（OSStatus {status}）。")


def installed():
    return sys.platform == "darwin" and APP_PATH.exists() and default_handler().lower() == BUNDLE_ID


def app_info_plist(original):
    """在 osacompile 生成的 Info.plist 上补齐：独立 bundle id、声明 discourse: 协议、不显示 Dock 图标。"""
    return {
        **original,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "linuxdo-mcp-auth",
        "CFBundleURLTypes": [{"CFBundleURLName": "linuxdo-mcp auth callback",
                              "CFBundleURLSchemes": [SCHEME]}],
        "LSUIElement": True,
    }


def _run(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _as_literal(path):
    """嵌进 AppleScript 双引号字符串前转义反斜杠与引号。"""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def install():
    _require_macos()
    previous = default_handler()
    script = _APPLESCRIPT.format(dir=_as_literal(store.CACHE_DIR),
                                 file=_as_literal(store.CACHE_DIR / CALLBACK_FILE))
    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "handler.applescript"
        src.write_text(script)
        if APP_PATH.exists():
            shutil.rmtree(APP_PATH)
        _run("osacompile", "-o", str(APP_PATH), str(src))
    plist = APP_PATH / "Contents" / "Info.plist"
    plist.write_bytes(plistlib.dumps(app_info_plist(plistlib.loads(plist.read_bytes()))))
    _run("codesign", "--force", "--deep", "--sign", "-", str(APP_PATH))  # 改了 plist 需重新 ad-hoc 签名
    _run(_LSREGISTER, "-f", str(APP_PATH))
    # 只在第一次安装时记录原处理程序；重装时 previous 已经是自己，不能把记录冲掉
    if not store.read_json(STATE_FILE) and previous.lower() != BUNDLE_ID:
        store.write_json(STATE_FILE, {"previous_handler": previous})
    _set_default_handler(BUNDLE_ID)
    return {"installed": str(APP_PATH), "default_handler": default_handler(),
            "previous_handler": store.read_json(STATE_FILE).get("previous_handler", "")}


def uninstall():
    _require_macos()
    previous = store.read_json(STATE_FILE).get("previous_handler", "")
    if previous:
        _set_default_handler(previous)
    if APP_PATH.exists():
        subprocess.run([_LSREGISTER, "-u", str(APP_PATH)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(APP_PATH)
    store.remove(STATE_FILE)
    result = {"uninstalled": True, "default_handler": default_handler()}
    if not previous:
        result["note"] = "安装前没有别的 discourse: 处理程序，无需还原；系统会在下次打开此类链接时询问。"
    return result


def open_in_browser(url):
    """在默认浏览器打开（macOS 用 open，确保走你日常已登录的那个浏览器）。"""
    if sys.platform == "darwin":
        _run("open", url)
    else:
        import webbrowser

        webbrowser.open(url)


def wait_callback(since, timeout):
    """等小程序写入 since 之后的回调地址；读到后删除文件并返回。"""
    path = store.CACHE_DIR / CALLBACK_FILE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if os.path.getmtime(path) >= since:
                url = path.read_text().strip()
                if url:
                    path.unlink(missing_ok=True)
                    return url
        except FileNotFoundError:
            pass
        time.sleep(1)
    raise RuntimeError(
        "等待授权超时：请在浏览器打开的授权页点「授权」，并允许打开 linuxdo-mcp-auth 后重试。")
