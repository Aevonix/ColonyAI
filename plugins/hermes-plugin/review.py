"""Keep autonomous skill changes in Hermes' existing proposal mechanism."""
import json
import hashlib


def editable_operation(arguments):
    """One native main-file edit, including its current single-op batch form."""
    operations = arguments.get('operations')
    if operations is not None:
        if not isinstance(operations, list) or len(operations) != 1 or not isinstance(operations[0], dict):
            return None
        operation = dict(operations[0])
        operation['name'] = operation.get('name') or arguments.get('name')
    else:
        operation = arguments
    if operation.get('action') not in {'edit', 'patch'} or operation.get('file_path') not in {None, '', 'SKILL.md'}:
        return None
    if operation.get('content'):
        if not isinstance(operation['content'], str) or operation.get('old_string') or operation.get('new_string') is not None:
            return None
    elif (operation.get('action') != 'patch' or not isinstance(operation.get('old_string'), str)
            or not operation['old_string'] or not isinstance(operation.get('new_string'), str)
            or type(operation.get('replace_all', False)) is not bool):
        return None
    return operation


def stage_skill_change(arguments):
    """Native review proposes; an owner or configured evaluator applies later.

    Foreground requests never use this adapter. Existing native ownership
    guards still run before staging and when the proposal is eventually applied.
    """
    from tools import skill_manager_tool as manager, write_approval as approval
    operations = arguments.get('operations')
    steps = operations if isinstance(operations, list) else [arguments]
    for operation in steps:
        if not isinstance(operation, dict):
            return json.dumps({'success':False, 'error':'Skill operation must be an object'})
        name = operation.get('name') or arguments.get('name', '')
        denied = manager._background_review_preflight(operation.get('action'), name)
        if denied is not None:
            return json.dumps(denied)
    summary = ('Review skill operation batch' if operations is not None else
               approval.skill_gist(arguments.get('action', ''), arguments.get('name', ''),
                   content=arguments.get('content') or '', file_path=arguments.get('file_path') or '',
                   old_string=arguments.get('old_string') or '', new_string=arguments.get('new_string') or ''))
    payload = dict(arguments)
    operation = editable_operation(arguments)
    if operation is not None:
        current = manager._find_skill(operation.get('name', ''))
        if current:
            payload['_colony_review_base_sha256'] = hashlib.sha256((current['path'] / 'SKILL.md').read_bytes()).hexdigest()
    record = approval.stage_write(approval.SKILLS, payload, summary=summary, origin='background_review')
    # Native staging is best-effort. Never report a stored proposal when its
    # writer failed; the later evaluator also reads this same pending record.
    if approval.get_pending(approval.SKILLS, record['id']) != record:
        return json.dumps({'success':False, 'error':'Native skill proposal was not persisted'})
    return json.dumps({'success':True, 'staged':True, 'pending_id':record['id'], 'gist':summary,
                      'message':'Skill change proposed for independent evaluation or owner review; active skills unchanged'})
