import pytest

from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.tools.registry import ToolRegistry


@pytest.fixture
def env():
    return SimulationEnvironment("TEST-001", "public-a")


@pytest.fixture
def registry(env):
    return ToolRegistry(env)
