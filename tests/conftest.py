import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import seed  # noqa: E402
from tests.helpers import Env  # noqa: E402


@pytest.fixture(scope="session")
def template(tmp_path_factory):
    p = tmp_path_factory.mktemp("tpl") / "template.db"
    h = seed.build_template(p)
    return p, h


@pytest.fixture
def env(tmp_path, template):
    return Env(tmp_path, template[0])


@pytest.fixture
def s(env):
    return env.new()
