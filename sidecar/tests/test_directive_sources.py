"""Task limits stay local; lasting rules use current canonical owner evidence."""
from contextlib import closing
import json
import pytest
from colony_sidecar.directives import DirectiveManager, DirectiveStore, Action
from colony_sidecar.directives.extractor import extract_directives
from colony_sidecar.turns import TurnIdempotencyLedger

@pytest.fixture
def setup(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turns.db')
    return ledger, DirectiveManager(DirectiveStore(str(tmp_path/'directives.db'), ledger=ledger))

def capture(setup,text,identifier='source-a'):
    ledger, manager = setup
    ledger.record_source(identifier,contact_id='owner',session_id='session-'+identifier,
        messages=[{'role':'user','content':text}],derive_claims=False)
    return manager.capture_from_message(text,source_id=identifier,contact_id='owner')

@pytest.mark.parametrize('text', [
    'Read the allowed documents. For this task, do not draft the final handoff yet. These are temporary task requirements, not a personal preference.',
    'Prepare the release report. This temporary task instruction is not a personal preference. Do not delegate or schedule yet.',
    'Please read the summary, but do not deploy the widget-service.',
    'For this task, never touch the widget-service.',
    'The example says: never send the report.',
    'I never sent anyone into that building.',
])
def test_temporary_requests_do_not_change_future_tool_scope(setup,text):
    assert capture(setup,text).captured == []
    ledger,manager=setup
    assert ledger.source_references(['source-a'],contact_id='owner',session_id='')
    assert manager.store.active() == [] and manager.context_brief() == ''
    assert manager.guard.check(Action(kind='execute_tool',text='draft the final handoff and deploy widget-service')).allowed


def test_exact_lasting_clause_persists_without_unrelated_content_or_level_escalation(setup):
    result=capture(setup,'From now on, do not modify the widget-service.\nRead the supplied documents.\nTEST_CREDENTIAL=not-a-real-secret')
    assert len(result.captured)==1
    ledger,manager=setup
    reopened=DirectiveManager(DirectiveStore(manager.store._db_path,ledger=ledger))
    rule=reopened.store.active()[0]
    assert rule.raw_text=='From now on, do not modify the widget-service.' and rule.level.value=='act'
    assert rule.evidence['source_turn_id']=='source-a'
    assert not reopened.guard.check(Action(kind='execute_tool',text='modify widget-service')).allowed
    assert reopened.guard.check(Action(kind='read',text='modify widget-service')).allowed
    assert 'TEST_CREDENTIAL' not in reopened.context_brief()
    stored=dict(manager.store._conn.execute('SELECT * FROM directives').fetchone())
    assert stored['subject']==stored['raw_text']==stored['match_terms']==''
    assert 'widget-service' not in json.dumps(stored) and 'TEST_CREDENTIAL' not in json.dumps(stored)


def test_forget_removes_context_effect_and_duplicate_text(setup):
    rule=capture(setup,'Never modify the confidential-widget-repo.').captured[0]
    ledger,manager=setup
    ledger.erase_sources(contact_id='owner',turn_ids=['source-a'])
    assert manager.store.active()==[] and manager.store.get(rule.id) is None and manager.context_brief()==''
    assert manager.consume_ack() is None
    assert manager.guard.check(Action(kind='execute_tool',text='modify confidential-widget-repo')).allowed
    with closing(ledger._connect()) as db:
        assert db.execute('SELECT 1 FROM turn_sources WHERE turn_id=?',('source-a',)).fetchone() is None
    assert 'confidential-widget-repo' not in json.dumps(dict(manager.store._conn.execute('SELECT * FROM directives').fetchone()))


def test_source_correction_withdraws_rule_and_annotation_forget_cannot_revive(setup):
    rule=capture(setup,'From now on, do not deploy the widget-service.').captured[0]
    ledger,manager=setup
    note=ledger.append_source_annotation(contact_id='owner',session_id='session-source-a',annotation_id='correction-a',
        source_id='source-a',source_version=rule.evidence['source_version'],excerpt=rule.raw_text,
        correction='That restriction was for the previous task only.',author_principal='owner-operator')
    assert manager.store.active()==[]
    assert manager.guard.check(Action(kind='execute_tool',text='deploy widget-service')).allowed
    ledger.erase_sources(contact_id='owner',turn_ids=[note['source_id']])
    assert manager.store.active()==[]


def test_source_identity_and_message_cannot_be_rebound(setup):
    ledger,manager=setup
    text='Never deploy the widget-service.'
    ledger.record_source('other',contact_id='other-person',session_id='other-session',messages=[{'role':'user','content':text}])
    assert manager.capture_from_message(text,source_id='other',contact_id='owner').captured==[]
    capture(setup,text)
    assert manager.capture_from_message('Never read widget-service.',source_id='source-a',contact_id='owner').captured==[]


def test_manual_intent_and_standalone_global_pause_remain(setup):
    ledger,manager=setup
    manual=manager.add_explicit('the widget-service',raw_text='Do not touch the widget-service.')
    assert manual.evidence is None and manager.store.get(manual.id)
    assert len(capture(setup,'Please pause your autonomy for now.').captured)==1
    assert not manager.guard.check(Action(kind='execute_tool',text='unrelated work')).allowed
    assert extract_directives('The user wrote "pause autonomy" as an example.')==[]



@pytest.mark.parametrize('text,expected', [
    ('From now on, do not modify widget-service, TEST_CREDENTIAL=not-a-real-secret',
     'From now on, do not modify widget-service'),
    ('Always check the deployment report because TEST_CREDENTIAL=not-a-real-secret',
     'Always check the deployment report'),
])
def test_only_rule_clause_is_quoted(setup, text, expected):
    rule = capture(setup, text).captured[0]
    assert rule.raw_text == expected
    assert 'TEST_CREDENTIAL' not in setup[1].context_brief()


@pytest.mark.parametrize('text', [
    'From now on, TEST_CREDENTIAL=not-a-real-secret.',
    'Never have I used that credential.',
    'From now on, my password is an unrelated test string.',
])
def test_standing_prefix_does_not_make_factual_text_a_rule(setup, text):
    assert capture(setup, text).captured == []


def test_forget_withdraws_recent_block_and_pending_lift(setup):
    capture(setup, 'Never deploy the widget-service.')
    ledger, manager = setup
    assert not manager.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
    assert manager.guard.recent_blocks()
    assert capture(setup, 'Resume deploying widget-service.', 'lift-request').needs_confirmation
    assert manager.pending_confirmation()
    ledger.erase_sources(contact_id='owner', turn_ids=['source-a'])
    assert manager.guard.recent_blocks() == []
    assert manager.pending_confirmation() is None
    assert capture(setup, 'yes', 'confirmation').revoked == []


@pytest.mark.asyncio
async def test_real_owner_turn_ingestion_and_manual_api(monkeypatch, tmp_path):
    """Exercise the actual producer, source write, capture and future context."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from colony_sidecar.api.routers import host
    from colony_sidecar.api.middleware import ApiKeyMiddleware
    from colony_sidecar import identity
    from colony_sidecar.events import broadcaster
    from colony_sidecar.turns import get_turn_idempotency_ledger
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('COLONY_DIRECTIVE_LLM_ASSIST', 'true')  # Retired setting has no model consumer.
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    for name in ('_graph', '_contacts_store', '_presence_store', '_context_provenance',
                 '_telemetry', '_p8_runtime', '_reranker', '_context_recall_selector',
                 '_preference_learner', '_world_populator', '_conversation_extractor',
                 '_relationship_profiler', '_tom_extractor'):
        monkeypatch.setattr(host, name, None)
    monkeypatch.setattr(identity, 'get_owner_contact_id', lambda: 'owner')
    events = []
    monkeypatch.setattr(broadcaster, 'emit', lambda kind, payload: events.append((kind, payload)))
    ledger = get_turn_idempotency_ledger(tmp_path)
    manager = DirectiveManager(DirectiveStore(str(tmp_path/'directives.db'), ledger=ledger))
    monkeypatch.setattr(host, '_directive_manager', manager)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key='fixture-token')
    app.include_router(host.router)
    app.include_router(host.v2_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                           headers={'Authorization': 'Bearer fixture-token'}) as client:
        async def turn(identifier, content):
            response = await client.put('/v2/host/turns/'+identifier, json={
                'identity': {'host_id': 'fixture-host'},
                'context': {'session_id': 'session-'+identifier, 'contact_id': 'owner',
                            'channel_id': 'test:thread', 'turn_id': identifier},
                'user_message': {'role': 'user', 'content': content},
                'assistant_message': {'role': 'assistant', 'content': 'Understood.'}})
            assert response.status_code == 201, response.text
            assert response.json()['source_recorded'] is True
        await turn('temporary-task', 'Read the report. For this task, do not deploy widget-service. This is not a personal preference.')
        assert manager.store.active() == []
        assert manager.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
        assert ledger.source_references(['temporary-task'], contact_id='owner', session_id='')
        await turn('lasting-rule', 'From now on, do not deploy widget-service. Read the report. TEST_CREDENTIAL=not-a-real-secret')
        rule = manager.store.active()[0]
        assert rule.evidence['source_turn_id'] == 'lasting-rule'
        assert rule.raw_text == 'From now on, do not deploy widget-service.'
        assert not manager.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
        assert manager.guard.check(Action(kind='read', text='deploy widget-service')).allowed
        capture_event = [payload for kind, payload in events if kind == 'directive.captured']
        assert capture_event == [{'captured_ids': [rule.id], 'revoked_ids': [], 'needs_confirmation': False}]
        assert 'widget-service' not in json.dumps(capture_event)
        context = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture-host'},
            'context': {'session_id': 'later-channel-session', 'contact_id': 'owner'},
            'incoming_message': {'role': 'user', 'content': 'Inspect the build.'}})
        assert context.status_code == 200, context.text
        boundaries = '\n'.join(section['body'] for section in context.json()['sections']
                               if 'standing boundaries' in section['body'])
        assert rule.raw_text in boundaries
        assert 'Read the report' not in boundaries and 'TEST_CREDENTIAL' not in boundaries
        ledger.erase_sources(contact_id='owner', turn_ids=['lasting-rule'])
        assert (await client.get('/v1/host/directives')).json()['directives'] == []
        assert manager.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
        manual = await client.post('/v1/host/directives', json={
            'subject': 'widget-service', 'raw_text': 'Do not touch widget-service.'})
        assert manual.status_code == 200 and manual.json()['stored'] is True
        assert manager.store.get(manual.json()['id']).evidence is None
        assert not manager.guard.check(Action(kind='execute_tool', text='modify widget-service')).allowed


@pytest.mark.parametrize('text', [
    'Never deploy widget-service unless I approve.',
    'From now on, do not deploy widget-service before I approve.',
    'Never send widget-service reports without checking with me.',
])
def test_conditions_are_not_discarded_to_make_unconditional_rules(setup, text):
    assert capture(setup, text).captured == []
    assert setup[1].guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed


def test_dotted_identifier_remains_exact_and_source_bound_logs_have_no_prose(setup, caplog):
    import logging
    caplog.set_level(logging.INFO)
    rule = capture(setup, 'Never read secrets.env.').captured[0]
    assert rule.raw_text == 'Never read secrets.env.'
    assert rule.subject == 'read secrets.env'
    assert not setup[1].guard.check(Action(kind='read', text='read secrets.env')).allowed
    assert rule.id in caplog.text and 'secrets.env' not in caplog.text


@pytest.mark.parametrize('text', [
    'Never forget to check the backup.',
    'From now on, do not fail to check the backup.',
    'Never stop checking the backup.',
])
def test_negative_complements_do_not_prohibit_the_underlying_action(setup, text):
    assert capture(setup, text).captured == []
    assert setup[1].guard.check(Action(kind='execute_tool', text='check the backup')).allowed


def test_repeated_identical_clause_is_acknowledged_once_and_stays_enforced(setup):
    result = capture(setup, 'Never deploy widget-service. Never deploy widget-service.')
    assert len(result.captured) == 1
    ledger, manager = setup
    reopened = DirectiveManager(DirectiveStore(manager.store._db_path, ledger=ledger))
    assert len(reopened.store.active()) == 1
    assert not reopened.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
    ledger.erase_sources(contact_id='owner', turn_ids=['source-a'])
    assert reopened.store.active() == []


def test_source_bound_verdict_has_no_duplicate_prose_for_downstream_storage(setup):
    rule = capture(setup, 'Never deploy confidential-widget-service.').captured[0]
    ledger, manager = setup
    verdict = manager.guard.check(Action(kind='execute_tool', text='deploy confidential-widget-service'))
    assert not verdict.allowed and rule.id in verdict.reason
    serialized = json.dumps(verdict.as_dict())
    assert 'confidential-widget-service' not in serialized
    assert verdict.as_dict()['violations'] == [{'id': rule.id}]
    ledger.erase_sources(contact_id='owner', turn_ids=['source-a'])
    assert manager.store.get(rule.id) is None
    assert 'confidential-widget-service' not in serialized


@pytest.mark.parametrize('text', [
    'Never again did I deploy widget-service.',
    'Never before today had we deployed widget-service.',
])
def test_adverbial_past_fact_does_not_become_a_standing_rule(setup, text):
    assert capture(setup, text).captured == []
    assert setup[1].guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed


def test_temporary_instructions_as_object_do_not_cancel_a_lasting_rule(setup):
    rule = capture(setup, 'Never delete temporary task instructions.').captured[0]
    assert setup[1].store.get(rule.id)
    assert not setup[1].guard.check(Action(kind='execute_tool', text='delete temporary task instructions')).allowed


@pytest.mark.parametrize('withdrawal', ['erase', 'correct'])
def test_erased_or_corrected_lift_request_cannot_be_confirmed(setup, withdrawal):
    rule = capture(setup, 'Never deploy widget-service.').captured[0]
    ledger, manager = setup
    assert capture(setup, 'Resume deploying widget-service.', 'lift').needs_confirmation
    proof = manager._pending_lift['evidence']
    if withdrawal == 'erase':
        ledger.erase_sources(contact_id='owner', turn_ids=['lift'])
    else:
        ledger.append_source_annotation(contact_id='owner', session_id='session-lift', annotation_id='lift-correction',
            source_id='lift', source_version=proof['source_version'], excerpt='Resume deploying widget-service.',
            correction='That request was mistaken. Keep the restriction.', author_principal='owner-operator')
    assert manager.pending_confirmation() is None
    assert capture(setup, 'yes', 'unrelated-yes').revoked == []
    assert manager.store.get(rule.id) is not None
    assert not manager.guard.check(Action(kind='execute_tool', text='deploy widget-service')).allowed
