import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import seed  
from tests.helpers import Env  


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


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Tests must never call the paid model or read a developer's .env, whatever their shell or project folder holds."""
    for k in [k for k in os.environ if k.startswith("NEBIUS_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("USUAL_NO_DOTENV", "1")