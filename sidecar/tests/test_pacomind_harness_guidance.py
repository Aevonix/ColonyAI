"""Generated guidance and skill discovery use one canonical installation."""
from pacomind.harness_integration import (
    PACOMIND_CONTEXT_TEMPLATE, write_pacomind_context, write_pacomind_skill,
)
from pacomind.harness_integration import skills


def test_context_generation_preserves_unrelated_private_instructions(tmp_path):
    identity = tmp_path / 'SOUL.md'
    identity.write_text('Private agent identity.')
    assert write_pacomind_context(tmp_path)
    assert (tmp_path / 'PACOMIND.md').read_text() == PACOMIND_CONTEXT_TEMPLATE
    assert identity.read_text() == 'Private agent identity.'


def test_skill_install_updates_one_discovered_directory(tmp_path, monkeypatch):
    target = tmp_path / 'pacomind-diagnose'
    monkeypatch.setitem(skills.SKILL_PATHS, 'codex', str(target))
    assert write_pacomind_skill('codex')
    (target / 'SKILL.md').write_text('outdated instruction')
    assert write_pacomind_skill('codex')
    assert list(tmp_path.iterdir()) == [target]
    assert 'name: pacomind-diagnose' in (target / 'SKILL.md').read_text()
