import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentpay.testing import TRUSTED, World, make_intent  # noqa: E402,F401


@pytest.fixture
def world():
    return World()
