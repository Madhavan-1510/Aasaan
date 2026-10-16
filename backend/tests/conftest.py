import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Store  # noqa: E402
from app.orchestrator import Swarm  # noqa: E402
from app.router import ModelRouter  # noqa: E402

SAMPLE = """Motion is the change in position of an object with time. An object is at rest if its position does not change.

Speed is the distance travelled per unit time. Speed has only magnitude and no direction. A car covering 60 km in 1 hour has a speed of 60 km/h.

Velocity is speed in a given direction. Velocity has both magnitude and direction. A car moving 60 km/h towards north has a velocity of 60 km/h north.

Acceleration is the rate of change of velocity with time. If velocity changes by 10 m/s in 5 s the acceleration is 2 m/s squared."""


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
def swarm(store, router):
    return Swarm(store, router)
