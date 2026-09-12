"""Actual gateway task tools, concurrent roots and cross-channel control.

SDK responses are controlled. This qualifies native integration and provenance,
not model obedience, physical messaging, or production performance.
"""
import importlib.util
import os
from pathlib import Path

import pytest

from conftest import ROOT, run_python


def test_native_task_channels(artifacts, tmp_path):
    native = os.environ.get('PACOMIND_TEST_HERMES_PATH', '')
    if not native and importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes for native gateway integration')
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), PACOMIND_STATE_DIR=str(tmp_path/'state'),
        HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'), HERMES_DISABLE_TELEMETRY='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', PACOMIND_SKIP_DOTENV='1', PYTHON_DOTENV_DISABLED='1',
        PACOMIND_GENERAL_PLUGIN_ACTIVE='1', PACOMIND_MEMORY_WORKER_TOOLS='0',
        PACOMIND_MEMORY_TURN_WRITER='disabled', PACOMIND_GUARD_CHAT_MODE='off',
        PACOMIND_INTROSPECTION_ENABLED='false', PACOMIND_RECALL_RERANK='off',
        OPENAI_API_KEY='fixture-model-key', OPENAI_BASE_URL='http://model.fixture/v1',
        LITELLM_LOCAL_MODEL_COST_MAP='True', GATEWAY_ALLOWED_USERS='owner-api',
        WHATSAPP_ALLOWED_USERS='15550003@s.whatsapp.net')
    result = run_python('-I', Path(__file__).with_name('native_task_channels_probe.py'),
        artifacts[3], ROOT/'sidecar', native,
        os.environ.get('PACOMIND_TEST_DEPENDENCY_PATH', ''), cwd=tmp_path, env=env)
    assert '"cross_channel_native_tasks": true' in result.stdout
