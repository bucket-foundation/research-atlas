import pytest


@pytest.fixture(autouse=True)
def tombstone_test_key(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.getbasetemp() / "tombstone-test.key"
    if not path.exists():
        path.write_bytes(b"k" * 32)
    monkeypatch.setenv("RESEARCH_ATLAS_TOMBSTONE_KEY", str(path))
    return path
