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
