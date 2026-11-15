import pytest

from grader.demo import create_demo
from grader.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'data')


@pytest.fixture
def project(store):
    return create_demo(store)
