import pytest


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("BAITD_DATA", str(tmp_path))
    yield tmp_path
