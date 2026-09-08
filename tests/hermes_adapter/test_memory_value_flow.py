"""A rejected undertaking must not continue through ordinary native tools."""
import test_commitment_work as coordination


def test_rejected_native_claim_cannot_continue_same_undertaking(artifacts, tmp_path, monkeypatch):
    # Reuse the actual two-session race, durable HTTP coordinator and packaged
    # Hermes plugin. This adds the consumer boundary absent from the original
    # race: the losing session tries to do the exact work it just failed to claim.
    anchor = "assert work(loser, 'status')['session_id'] == winner"
    check = '''
losing_calls = []
denied = run_tool_execution_middleware('read_file', {}, lambda args: losing_calls.append(args),
    session_id=loser, task_id=loser, turn_id=loser)
assert losing_calls == [], 'losing claimant executed the same undertaking'
assert json.loads(denied)['effect_performed'] is False, denied
start('unrelated-next-turn')
assert run_tool_execution_middleware('read_file', {}, lambda args: 'unrelated',
    session_id='unrelated-next-turn', task_id='unrelated-next-turn',
    turn_id='unrelated-next-turn') == 'unrelated'
assert work(loser, 'release')['detached'] is True
assert work(loser, 'status')['session_id'] == winner
assert run_tool_execution_middleware('read_file', {}, lambda args: 'detached',
    session_id=loser, task_id=loser, turn_id=loser) == 'detached'
'''
    assert coordination.PROBE.count(anchor) == 1
    probe = coordination.PROBE.replace(anchor, anchor + check)
    finished = "print(json.dumps({'native_race': True, 'recovery_fenced': True}))"
    assert probe.count(finished) == 1
    probe = probe.replace(finished, "assert work(loser, 'claim')['accepted']\n"
                          "assert work(loser, 'release')['accepted']\n" + finished)
    monkeypatch.setattr(coordination, 'PROBE', probe)
    coordination.test_native_sessions_share_one_undertaking(artifacts, tmp_path, monkeypatch)
