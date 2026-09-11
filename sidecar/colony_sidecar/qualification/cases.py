"""Small neutral cases. A role completion is not native agent qualification."""
import json

from .records import CaseSpec


async def role_completion(inputs, context):
    response = await context.router.complete(inputs['messages'], context={
        'function_role': inputs['role'], 'allow_fallback': False,
        'max_output_tokens': inputs.get('max_output_tokens', 512)})
    return {'output': response.content, 'effects': {}}


def json_fields(observed, oracle):
    """Exact independently specified fields, not a model judging itself."""
    output = observed.get('output')
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (ValueError, TypeError):
            return {'output_is_json': False}
    root = {**observed, 'output': output}
    checks = {}
    for item in oracle['fields']:
        current = root
        try:
            for part in item['path']:
                current = current[part]
        except (KeyError, IndexError, TypeError):
            checks[item['name']] = False
        else:
            checks[item['name']] = type(current) is type(item['equals']) and current == item['equals']
    return checks


STANDARD = [
    CaseSpec(id='chat.grounded-note', version='1', role='chat', boundary='role_completion',
        consumer='role_completion', evaluator='json_fields',
        inputs={'role': 'chat', 'messages': [
            {'role': 'system', 'content': 'Answer from the supplied note only. Return a JSON object; use null for information absent from the note.'},
            {'role': 'user', 'content': 'Note: The blue sample case is in drawer 4. The silver case was moved, but its new location is unknown. Where is each case? Use keys blue and silver.'}]},
        oracle={'fields': [
            {'name': 'known_location', 'path': ['output', 'blue'], 'equals': 'drawer 4'},
            {'name': 'unknown_preserved', 'path': ['output', 'silver'], 'equals': None}]}),
    CaseSpec(id='extraction.conditions', version='1', role='extraction', boundary='role_completion',
        consumer='role_completion', evaluator='json_fields',
        inputs={'role': 'extraction', 'messages': [
            {'role': 'system', 'content': 'Extract only explicit information. Return JSON with keys item, location, valid_day, requires_wet_shelf (boolean), owner. Use null for absent information.'},
            {'role': 'user', 'content': 'For tomorrow only, put the blue sample case in drawer 4 if the shelf is wet. Nobody has identified its owner.'}]},
        oracle={'fields': [
            {'name': 'item', 'path': ['output', 'item'], 'equals': 'blue sample case'},
            {'name': 'location', 'path': ['output', 'location'], 'equals': 'drawer 4'},
            {'name': 'time_condition', 'path': ['output', 'valid_day'], 'equals': 'tomorrow'},
            {'name': 'wet_shelf_condition', 'path': ['output', 'requires_wet_shelf'], 'equals': True},
            {'name': 'unknown_owner', 'path': ['output', 'owner'], 'equals': None}]}),
]

CONSUMERS = {'role_completion': role_completion}
EVALUATORS = {'json_fields': json_fields}

from .memory_cases import CASES as MEMORY_CASES, CONSUMERS as MEMORY_CONSUMERS, EVALUATORS as MEMORY_EVALUATORS
STANDARD.extend(MEMORY_CASES)
CONSUMERS.update(MEMORY_CONSUMERS)
EVALUATORS.update(MEMORY_EVALUATORS)


def select_cases(roles):
    cases = [case for case in STANDARD if case.role in roles]
    uncovered = set(roles) - {case.role for case in cases}
    if uncovered:
        raise ValueError('No installed standard cases for roles: ' + ', '.join(sorted(uncovered)))
    return cases
