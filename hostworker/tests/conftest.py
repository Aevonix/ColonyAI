import json
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

VECTORS_PATH = Path(__file__).resolve().parent / "vectors" / "golden_vectors.json"


@pytest.fixture(scope="session")
def golden_vectors() -> dict:
    with open(VECTORS_PATH, encoding="utf-8") as handle:
        vectors = json.load(handle)
    assert vectors["schema"] == "ColonyHostWorkerGoldenVectorsV1"
    assert vectors["version"] == 1
    return vectors
