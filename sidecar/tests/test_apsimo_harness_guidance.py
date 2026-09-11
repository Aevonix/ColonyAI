"""Generated guidance uses current commands without duplicating old skill installs."""
from pathlib import Path

from apsimo.harness_integration import (
    APSIMO_CONTEXT_TEMPLATE, COLONY_CONTEXT_TEMPLATE,
    write_apsimo_context, write_colony_context,
    write_apsimo_skill, write_colony_skill,
)
from apsimo.harness_integration import skills


def test_context_alias_and_private_history_preservation(tmp_path):
    old = tmp_path / 'COLONY.md'
    old.write_text('Private historical notes retained verbatim.')
    assert write_colony_context is write_apsimo_context
    assert COLONY_CONTEXT_TEMPLATE is APSIMO_CONTEXT_TEMPLATE
    assert write_apsimo_context(tmp_path)
    assert (tmp_path / 'APSIMO.md').read_text() == APSIMO_CONTEXT_TEMPLATE
    assert old.read_text() == 'Private historical notes retained verbatim.'


def test_legacy_skill_directory_is_reused_without_duplicate_discovery(tmp_path, monkeypatch):
    canonical = tmp_path / 'apsimo-diagnose'
    legacy = tmp_path / 'colony-diagnose'
    legacy.mkdir()
    monkeypatch.setitem(skills.SKILL_PATHS, 'codex', str(canonical))
    assert write_colony_skill is write_apsimo_skill
    assert write_apsimo_skill('codex')
    assert not canonical.exists()
    assert 'name: apsimo-diagnose' in (legacy / 'SKILL.md').read_text()
