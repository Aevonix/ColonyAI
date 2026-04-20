# Colony integration test configuration
import os
import pytest

# Skip all integration tests if COLONY_URL is not set (e.g., in CI without a sidecar)
colony_url = os.environ.get("COLONY_URL", "")

if not colony_url:
    pytestmark = pytest.mark.skip(
        reason="COLONY_URL not set — integration tests require a running Colony sidecar"
    )
