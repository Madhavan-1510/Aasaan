import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import web  # noqa: E402
import app.main  # noqa: E402,F401  (imported so the fixture can patch load_env)
from app.db import Store  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.router import ModelRouter  # noqa: E402
from app.worker import Worker  # noqa: E402

SAMPLE = """Motion is the change in position of an object with time. An object is at rest if its position does not change with time with respect to a reference point.

Speed is the distance travelled per unit time. Speed has only magnitude and no direction. A car covering 60 km in 1 hour has a speed of 60 km/h. The SI unit of speed is metre per second.

Velocity is speed in a given direction. Velocity has both magnitude and direction. A car moving 60 km/h towards north has a velocity of 60 km/h north. Two cars with the same speed can have different velocities if they move in different directions.

Acceleration is the rate of change of velocity with time. If velocity changes by 10 m/s in 5 s the acceleration is 2 m/s squared. An object moving with a constant velocity has zero acceleration.

In a distance-time graph, the slope gives the speed of the object. A horizontal line in a distance-time graph shows that the object is at rest. In a velocity-time graph, the area under the graph gives the displacement."""


class FakeResp:
    def __init__(self, status=200, text="", data=None, headers=None, url=""):
        self.status_code, self.text, self._data, self.url = status, text, data, url
        self.headers = {"content-type": "text/html"} if headers is None else headers

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeWeb:
    """Replaces web.http_get/http_post. Add routes: fake.get(lambda url, params: FakeResp(...) or None)."""

    def __init__(self):
        self.routes, self.calls = [], []

    def get(self, pred):
        self.routes.append(pred)

    def __call__(self, url, *, headers=None, params=None, timeout=0):
        self.calls.append((url, params))
        for pred in reversed(self.routes):
            r = pred(url, params or {})
            if r is not None:
                if not r.url:
                    r.url = url
                return r
        if url.endswith("/robots.txt"):
            return FakeResp(404, "")
        raise RuntimeError(f"unexpected network call in test: {url}")


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    """Tests never read the real .env and never see real API keys, so nothing can reach a paid service."""
    import app.main
    monkeypatch.setattr(app.main, "load_env", lambda *a, **k: None)
    for k in ("GEMINI_API_KEY", "SARVAM_API_KEY", "TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "YOUTUBE_API_KEY",
              "JINA_API_KEY", "FIRECRAWL_API_KEY", "AASAAN_ADMIN_TOKEN", "AASAAN_MODE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def fresh_tool_runner(monkeypatch):
    """Each test gets its own ToolRunner (no shared breakers or budgets) that never really sleeps between retries."""
    from app.tools import ToolRunner
    runner = ToolRunner(None, rate_scale=1000)
    runner.sleep = lambda s: None
    monkeypatch.setattr(web, "RUNNER", runner)
    return runner


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    """No test touches the real network. Tests add routes to this fake when they need web results."""
    fw = FakeWeb()
    monkeypatch.setattr(web, "http_get", fw)
    monkeypatch.setattr(web, "http_post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network blocked in tests")))
    monkeypatch.setattr(web, "_resolve", lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))])
    web._robots.clear()
    for k in ("YOUTUBE_API_KEY", "JINA_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return fw


@pytest.fixture
def sample_text():
    return SAMPLE


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def router(store):
    return ModelRouter(store=store, mode="mock")


@pytest.fixture
def engine(store, router, tmp_path):
    return Engine(store, router, upload_dir=tmp_path / "uploads", retry_delay_s=0)


@pytest.fixture
def worker(engine):
    return Worker(engine)
