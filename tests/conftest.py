import pytest

from missioncrew import seed as seed_mod
from missioncrew.engine import Engine
from missioncrew.store import Store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # 工作区与数据库都落在临时目录,互不干扰
    monkeypatch.setenv("MISSIONCREW_HOME", str(tmp_path / "mc_home"))
    return Store(tmp_path / "mc_home" / "db.sqlite3")


@pytest.fixture()
def seeded(store):
    seed_mod.seed(store)
    return store


@pytest.fixture()
def engine(seeded):
    return Engine(seeded)
