import pytest

from linuxdo_mcp import store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """每个用例用独立的缓存目录，且不受真实 LINUXDO_COOKIE 影响。"""
    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("LINUXDO_COOKIE", raising=False)
    return tmp_path


class FakeResponse:
    def __init__(self, status=200, text="{}", headers=None, cookies=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.cookies = cookies or {}

    def json(self):
        import json

        return json.loads(self.text)
