"""An older functional release must respect newer attribution invalidations."""
import hashlib
import json

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.identity.participants import ParticipantResolver
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia
from colony_sidecar.turns.source_vectors import chunks, hydrate
from test_source_media import image_bytes, message


def test_fresh_floor_has_normal_memory_without_new_feature_tables(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ledger.record_source('ordinary', contact_id='person', session_id='one',
                         messages=[{'role': 'user', 'content': 'The bicycle is turquoise.'}])
    assert ledger.search_sources('turquoise', contact_id='person', session_id='later')
    assert ledger.source_references(['ordinary'], contact_id='person', session_id='later')
    with ledger._connect() as conn:
        tables = {r[0] for r in conn.execute('SELECT name FROM sqlite_master')}
        assert 'source_attribution_invalidations' in tables
        assert not {'appraisal_records', 'appraisal_runs', 'source_attribution_operations', 'temporal_followups'} & tables


def test_reopen_after_correction_hides_copies_media_vectors_and_delayed_children(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ledger.record_source('direct', contact_id='old', session_id='original',
                         messages=[{'role': 'user', 'content': 'The kayak is turquoise.'}])
    direct_refs = ledger.source_references(['direct'], contact_id='old', session_id='original')
    image = message()
    copied = {**image, 'role': 'assistant', '_supplied_sources': direct_refs}
    copied['content'][0]['text'] = 'I remember your turquoise kayak.'
    ledger.record_source('copy', contact_id='old', session_id='other', messages=[copied])
    copy_refs = ledger.source_references(['copy'], contact_id='old', session_id='other')
    ledger.record_source('child', contact_id='old', session_id='third', messages=[{
        'role': 'assistant', 'content': 'Your turquoise kayak was discussed.', '_supplied_sources': copy_refs}])
    child_refs = ledger.source_references(['child'], contact_id='old', session_id='third')
    asset = hashlib.sha256(image_bytes()).hexdigest()
    assert SourceMedia(ledger).read(asset, contact_id='old', session_id='later')[0] == image_bytes()
    with ledger._connect() as conn:
        metadata = [meta for _, meta in chunks(conn, conn.execute("SELECT * FROM turn_sources WHERE turn_id='copy'").fetchone())]
        before = {r['turn_id']: r['messages_json'] for r in conn.execute('SELECT turn_id,messages_json FROM turn_sources')}
    assert hydrate(ledger, metadata[0], contact_id='old', session_id='later')
    # This is the durable state committed by the feature release's correction
    # transaction. The compatibility binary deliberately has no correction API.
    with ledger._connect() as conn, conn:
        conn.execute("UPDATE turn_sources SET contact_id='new' WHERE turn_id='direct'")
        for sid in ('copy', 'child'):
            conn.execute('INSERT INTO source_attribution_invalidations VALUES (?,?,?)', (sid, 'direct', 'owner-correction'))
            conn.execute('INSERT INTO source_projection_erasures VALUES (?,?)', (sid, 'direct'))
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert not reopened.search_sources('turquoise', contact_id='old', session_id='later')
    assert reopened.search_sources('turquoise', contact_id='new', session_id='later')
    assert not reopened.source_references(['copy', 'child'], contact_id='old', session_id='later')
    assert hydrate(reopened, metadata[0], contact_id='old', session_id='later') is None
    with pytest.raises(KeyError, match='unknown asset'):
        SourceMedia(reopened).read(asset, contact_id='old', session_id='later')
    with pytest.raises(ValueError, match='invalid_source_dependency'):
        reopened.record_source('delayed', contact_id='old', session_id='later', messages=[{
            'role': 'assistant', 'content': 'Your kayak is turquoise.', '_supplied_sources': child_refs}])
    with reopened._connect() as conn:
        assert {r['turn_id']: r['messages_json'] for r in conn.execute('SELECT turn_id,messages_json FROM turn_sources')} == before


@pytest.mark.asyncio
async def test_floor_name_suggestion_never_attributes_or_runs_identity_migration(tmp_path):
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / 'contacts.db')))
    await store.connect()
    try:
        target = await store.create(display_name='Robin', trust_tier='trusted')
        group = await store.create_scope(scope_type='group', platform='whatsapp', external_id='group')
        await store.add_scope_member(group.scope_id, target.contact_id)
        resolver = ParticipantResolver(store)
        first = await resolver.resolve(platform='whatsapp', user_id='synthetic@lid', display_name='Robin', group_id='group')
        assert first.method == 'shadow' and first.contact_id != target.contact_id and first.proposal_filed
        assert (await resolver.resolve(platform='whatsapp', user_id='synthetic@lid')).contact_id == first.contact_id
        assert await store.get_handles(target.contact_id) == []
        assert (await store.list_handle_proposals())[0]['contact_id'] == target.contact_id
        await store.add_handle(target.contact_id, 'email', 'guess@example.test', source='auto:scoped-name')
        assert await store.resolve_handle('email', 'guess@example.test') is None
        assert await store.resolve_messaging_handle('email', 'guess@example.test') is None
        tables = {r[0] for r in await (await store._require_db().execute('SELECT name FROM sqlite_master')).fetchall()}
        assert 'contact_identity_candidates' not in tables
        await store.add_handle(target.contact_id, 'sms', '+12125550101', verified=True)
        other = await store.create(display_name='Other')
        await store.add_handle(other.contact_id, 'signal', '+442125550101', verified=True)
        assert (await store.resolve_messaging_handle('rcs', '(212) 555-0101')).contact_id == target.contact_id
        assert (await store.resolve_messaging_handle('whatsapp', '442125550101@s.whatsapp.net')).contact_id == other.contact_id
    finally:
        await store.close()
