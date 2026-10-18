"""本地状态的存放位置与读写：会话 cookie、cf_clearance、client_id、专用浏览器 profile。

默认目录 ~/.cache/linuxdo-mcp（700），可用环境变量 LINUXDO_CACHE_DIR 改。
文件用 mkstemp 先写临时文件（创建即 600、名字唯一）再原子替换，不会被读到半截或短暂可读。
"""
import json
import os
import pathlib
import tempfile

CACHE_DIR = pathlib.Path(
    os.environ.get("LINUXDO_CACHE_DIR") or os.path.expanduser("~/.cache/linuxdo-mcp")
)


def ensure_dir():
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)
    return CACHE_DIR


def read_json(name):
    """读取缓存目录下的 JSON 文件；不存在或损坏时返回空 dict。"""
    try:
        data = json.loads((CACHE_DIR / name).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(name, data):
    ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE_DIR / name)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def remove(name):
    try:
        (CACHE_DIR / name).unlink()
    except FileNotFoundError:
        pass
