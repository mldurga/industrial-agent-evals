import pytest

from plant_evals.generate import write_plant
from plant_evals.store import PlantStore


@pytest.fixture(scope="session")
def plant_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("plant")
    write_plant(path, seed=42)
    return path


@pytest.fixture(scope="session")
def store(plant_dir):
    return PlantStore.load(plant_dir)
