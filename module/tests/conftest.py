import sys
from pathlib import Path

import pytest

# Tests import the module code the same way the container does (from module/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extract  # noqa: E402
from fake_datalogger import serve  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_status():
    extract.STOP.clear()
    for key in extract.STATUS:
        extract.STATUS[key] = 0 if isinstance(extract.STATUS[key], int) and not isinstance(extract.STATUS[key], bool) else None
    extract.STATUS["connected"] = False
    yield
    extract.STOP.clear()


@pytest.fixture
def logger_server():
    server, port, servicer = serve(0)
    yield f"localhost:{port}", servicer
    server.stop(None)
