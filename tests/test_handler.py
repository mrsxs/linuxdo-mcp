import os
import threading
import time

import pytest

from linuxdo_mcp import handler


def test_info_plist_declares_scheme_and_hides_dock_icon():
    plist = handler.app_info_plist({"CFBundleExecutable": "applet", "CFBundleIdentifier": "com.apple.ScriptEditor.id.x"})
    assert plist["CFBundleExecutable"] == "applet"
    assert plist["CFBundleIdentifier"] == handler.BUNDLE_ID
    assert plist["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["discourse"]
    assert plist["LSUIElement"] is True


def test_applescript_literal_escaping():
    assert handler._as_literal('/a "b"\\c') == '/a \\"b\\"\\\\c'


@pytest.fixture
def lsdb(monkeypatch):
    """替身 LaunchServices：state 里放当前 discourse: 处理程序、是否已装好、install 调用次数。"""
    state = {"handler": "", "installed": False, "installs": 0}
    monkeypatch.setattr(handler.sys, "platform", "darwin")
    monkeypatch.setattr(handler, "installed", lambda: state["installed"])
    monkeypatch.setattr(handler, "default_handler", lambda: state["handler"])

    def install():
        state["installs"] += 1

    monkeypatch.setattr(handler, "install", install)
    return state


def test_ensure_installed_installs_when_scheme_is_free(lsdb):
    assert handler.ensure_installed() is True
    assert lsdb["installs"] == 1


def test_ensure_installed_reinstalls_deleted_app(lsdb):
    lsdb["handler"] = handler.BUNDLE_ID.upper()  # 默认处理程序仍是自己，但 App 被删了
    assert handler.ensure_installed() is True
    assert lsdb["installs"] == 1


def test_ensure_installed_keeps_existing(lsdb):
    lsdb["installed"] = True
    assert handler.ensure_installed() is True
    assert lsdb["installs"] == 0


def test_ensure_installed_never_takes_over_other_apps(lsdb):
    lsdb["handler"] = "com.example.fluxdo"
    assert handler.ensure_installed() is False
    assert lsdb["installs"] == 0


def test_ensure_installed_skips_non_macos(lsdb, monkeypatch):
    monkeypatch.setattr(handler.sys, "platform", "linux")
    assert handler.ensure_installed() is False
    assert lsdb["installs"] == 0


def test_wait_callback_reads_and_removes_fresh_file(isolated_cache):
    path = isolated_cache / handler.CALLBACK_FILE
    started = time.time()
    threading.Timer(0.2, lambda: path.write_text("discourse://auth_redirect?payload=x\n")).start()
    assert handler.wait_callback(started, timeout=5) == "discourse://auth_redirect?payload=x"
    assert not path.exists()


def test_wait_callback_ignores_stale_file(isolated_cache, monkeypatch):
    path = isolated_cache / handler.CALLBACK_FILE
    path.write_text("discourse://auth_redirect?payload=old")
    os.utime(path, (1, 1))
    monkeypatch.setattr(handler.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="超时"):
        handler.wait_callback(time.time(), timeout=0.05)
