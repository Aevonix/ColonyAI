# Colony integration test configuration
import os
import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests if COLONY_URL is not set."""
    colony_url = os.environ.get("COLONY_URL", "")
    if not colony_url:
        skip = pytest.mark.skip(reason="COLONY_URL not set — integration tests require a running Colony sidecar")
        for item in items:
            item.add_marker(skip)
