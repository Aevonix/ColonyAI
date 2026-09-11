"""Prospective runtime observations for the existing working-judgment queue.

Called only after the server rereads an owned native task. No model report,
tool result prose, caller-supplied outcome or imported history is evidence here.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math

VERSION = 'native-runtime-observation-v1'


def retain_outcome(review, native, state, owner):
    binding = review.get('native_work') or {}
    selection = binding.get('outcome_learning') or {}
    observed = state.get('runtime_observation')
    if selection.get('version') != VERSION or not observed:
        return
    started, ended = observed['started_at'], observed['ended_at']
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (started, ended)):
        return
    # Native run timestamps have whole-second precision. The binding marker
    # is only set while this newly bound task has no runs.
    if ended < started or started < math.floor(datetime.fromisoformat(selection['bound_at']).timestamp()):
        return
    if observed['outcome'] not in {'crashed', 'timed_out', 'spawn_failed', 'gave_up'}:
        return
    identity = {**native, 'native_run_id': state['native_run_id']}
    source_id = 'native-runtime:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    facts = {**observed, 'duration_seconds': ended-started, 'identity': identity,
             'registered_action': review['review']['action']}
    message = {'role': 'assistant', '_native_runtime_observation': VERSION,
        'content': 'Runtime-owned internal review execution observation. '
            'This records lifecycle and elapsed time, not the accuracy of any output. '
            'Requested model configuration does not identify the serving processor.\n'
            + json.dumps(facts, sort_keys=True)}
    from apsimo.turns import get_turn_idempotency_ledger
    from apsimo import get_state_dir
    from apsimo.turns.idempotency import SourceErased
    ledger = get_turn_idempotency_ledger(get_state_dir())
    # Retain the first observation unchanged. Native task overrides can change
    # after a run; observing it again must not rewrite that earlier evidence.
    with closing(ledger._connect()) as conn:
        if conn.execute('SELECT 1 FROM turn_sources WHERE turn_id=?', (source_id,)).fetchone():
            return
    try:
        ledger.record_source(source_id, contact_id=owner,
            session_id='native-runtime:'+native['native_task_id'], messages=[message],
            occurred_at=datetime.fromtimestamp(ended, timezone.utc).isoformat(),
            derive_claims=False, runtime_judgment=True)
    except SourceErased:
        # Normal observation replay must not resurrect an erased experience.
        return
