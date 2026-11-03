import base64
import json
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import energipays  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DUMMY_KEY = base64.b64encode(b"k" * 32).decode()  # never the real key
DEVICE_ID = "00000000-0000-4000-8000-000000000001"
DATA = "https://data-au-1.energipays.com"


def fake_jwt(exp: float) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': int(exp)})}.sig"


class Resp:
    def __init__(self, status=200, body=None, encrypted=False):
        self.status_code = status
        self._body = body if body is not None else {}
        if encrypted:
            self._body = {"payload": energipays.encrypt(json.dumps(self._body))}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeSession:
    """Records every request; answers from a queue of (method, url-substring, Resp)."""

    def __init__(self):
        self.headers = {}
        self.cookies = {}
        self.calls = []
        self.routes = []

    def add(self, method, fragment, resp):
        self.routes.append((method, fragment, resp))

    def _go(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (m, frag, resp) in enumerate(self.routes):
            if m == method and frag in url:
                del self.routes[i]
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._go("GET", url, **kw)

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def put(self, url, **kw):
        return self._go("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._go("DELETE", url, **kw)


def sent_body(call):
    """Decrypt the payload of an encrypted form request."""
    import urllib.parse
    form = urllib.parse.parse_qs(call[2]["data"])
    return energipays.decrypt(form["payload"][0])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(energipays, "_TOKEN_CACHE", tmp_path / ".token_cache.json")
    monkeypatch.setattr(energipays, "_KEY_CACHE", tmp_path / ".key_cache.json")
    monkeypatch.setattr(energipays, "DATA_RETRY_DELAY", 0, raising=False)
    energipays.set_key(DUMMY_KEY)
    c = energipays.EnergipaysClient(email="user@example.com", password="x", auto_login=False)
    c.session = FakeSession()
    c._set_access_token(fake_jwt(time.time() + 3600))
    c._refresh_token = "r"
    return c


@pytest.fixture
def device_fixture():
    return json.loads((FIXTURES / "device.json").read_text())
