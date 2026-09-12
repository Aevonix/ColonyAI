"""Read the projected profile through the pinned native gateway and adapter."""
from copy import deepcopy
import socket

import pytest
import yaml

from pacomind.setup_hermes import _receipt_preference


LAYOUTS = [
    {'whatsapp': {'enabled': False, 'allow_from': ['fixture'], 'send_read_receipts': False}},
    {'whatsapp': {'enabled': True, 'reply_prefix': 'Fixture'}},
    {'platforms': {'whatsapp': {'enabled': False, 'extra': {'send_read_receipts': False, 'custom_option': 'retained'}}}},
    {'gateway': {'platforms': {'whatsapp': {'enabled': False, 'send_read_receipts': False, 'allow_from': ['fixture']}}}},
    {'gateway': {'whatsapp': {'enabled': False, 'extra': {'bridge_port': 3005}}}},
    {'whatsapp': {'enabled': False, 'send_read_receipts': False},
     'platforms': {'whatsapp': {'extra': {'send_read_receipts': True}}},
     'gateway': {'platforms': {'whatsapp': {'send_read_receipts': True}},
                 'whatsapp': {'extra': {'send_read_receipts': False}}}},
    {'gateway': {'platforms': {'whatsapp': {'enabled': False, 'allow_from': ['fixture'],
                                        'send_read_receipts': False}}},
     'platforms': {'whatsapp': {'send_read_receipts': True, 'extra': {'send_read_receipts': True}}}},
    # No explicit selection, but an existing connection configuration: adding
    # the preference cannot introduce the native "nonempty extras" signal.
    {'platforms': {'whatsapp': {'extra': {'bridge_port': 3005}}}},
]


def native_load(tmp_path, monkeypatch, config):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('WHATSAPP_ENABLED', 'false')
    def no_network(*args, **kwargs):
        raise AssertionError('Config readback must not contact a bridge or model')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    from gateway import config as native
    monkeypatch.setattr(native, 'get_hermes_home', lambda: tmp_path)
    (tmp_path/'config.yaml').write_text(yaml.safe_dump(config))
    return native.load_gateway_config().platforms.get(native.Platform.WHATSAPP)


@pytest.mark.parametrize('config', LAYOUTS)
@pytest.mark.parametrize('choice', ['on', 'off'])
def test_actual_native_receipt_projection_keeps_channel_selection(tmp_path, monkeypatch, config, choice):
    before = native_load(tmp_path, monkeypatch, config)
    candidate, _ = _receipt_preference(config, choice)
    after = native_load(tmp_path, monkeypatch, candidate)
    assert after.extra['send_read_receipts'] is (choice == 'on')
    expected, actual = deepcopy(before.to_dict()), deepcopy(after.to_dict())
    for row in (expected, actual):
        row.get('extra', {}).pop('send_read_receipts', None)
    assert actual == expected
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
    adapter = WhatsAppAdapter(after)
    assert adapter._bridge_env()['WHATSAPP_SEND_READ_RECEIPTS'] == ('true' if choice == 'on' else 'false')


def test_actual_unconfigured_native_channel_remains_unconfigured(tmp_path, monkeypatch):
    before = native_load(tmp_path, monkeypatch, {})
    with pytest.raises(ValueError, match='enabled: true or enabled: false'):
        _receipt_preference({}, 'on')
    after = native_load(tmp_path, monkeypatch, {})
    assert not before or not before.enabled
    assert not after or not after.enabled
