"""MCP 自己的 linux.do 登录会话（_t cookie）。

来源优先级：
1. session.json —— 授权登录（login）兑换来的独立会话，随响应轮换自续期
2. 环境变量 LINUXDO_COOKIE —— 手工指定的 _t（兜底；只在从未有过 session.json 时导入一次）
都没有时返回空串，按匿名身份访问公开内容。

logout 或服务端判定失效后，session.json 留一个「已注销」标记，不会再从环境变量把旧 _t 捡回来。

授权登录拿到的是一个全新的会话，和任何浏览器里的登录互不相干，
所以不会再出现「工具和浏览器共用同一个 _t、被 Discourse 轮换互相顶下线」的问题。
"""
import os
import threading
import time

from . import store

COOKIE_NAME = "_t"
FILE = "session.json"
LEGACY_FILE = "cookie.json"  # 0.2.x 存的是从浏览器读来的共享 _t，不再使用
_LOCK = threading.Lock()


def _normalize(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    return raw if "=" in raw else f"{COOKIE_NAME}={raw}"


def get_cookie():
    """返回可直接放进 Cookie 头的 `_t=...`；未登录时返回空串。"""
    data = store.read_json(FILE)
    if data:
        return _normalize(data.get("cookie"))
    env = _normalize(os.environ.get("LINUXDO_COOKIE"))
    if env:
        save(env)
    return env


def _update(**fields):
    with _LOCK:  # 读-改-写要串行，免得并发的轮换写回被旧值覆盖
        store.write_json(FILE, {**store.read_json(FILE), **fields, "ts": time.time()})
    store.remove(LEGACY_FILE)


def save(cookie, **extra):
    """保存会话 cookie；extra（如 username）与已有字段合并保留。"""
    _update(**extra, cookie=_normalize(cookie), logged_out=False)


def set_username(username):
    """只更新用户名，不碰 cookie（避免把并发轮换回来的新 _t 写回旧值）。"""
    if username:
        _update(username=username)


def clear():
    """注销本地会话：写一个空会话标记，而不是删文件，防止环境变量里的旧 _t 被重新导入。"""
    with _LOCK:
        store.write_json(FILE, {"cookie": "", "logged_out": True, "ts": time.time()})


def absorb_rotation(response):
    """Discourse 会定期轮换 _t。响应里带回新的 _t 就写回，实现自续期。"""
    jar = getattr(response, "cookies", None)
    token = jar.get(COOKIE_NAME) if jar is not None and hasattr(jar, "get") else None
    if token:
        save(f"{COOKIE_NAME}={token}")
