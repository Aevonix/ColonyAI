"""Transport-held undertaking tokens; no model-selected authority or effects."""
from collections import OrderedDict
import json
import threading
from urllib.parse import quote


class CommitmentCoordinator:
    def __init__(self, client):
        self.client = client
        self._lock = threading.RLock()
        self._claims = OrderedDict()
        self._children = OrderedDict()

    @staticmethod
    def _key(value):
        return tuple(str(value.get(key) or '') for key in ('session_id', 'task_id', 'turn_id'))

    def child(self, **kwargs):
        with self._lock:
            matches = [key for key in self._claims if key[0] == kwargs.get('parent_session_id')
                       and key[2] == kwargs.get('parent_turn_id')]
            child = str(kwargs.get('child_session_id') or '')
            if child and len(matches) == 1:
                self._children[child] = {**self._claims[matches[0]], "child_session_id": child}
                while len(self._children) > 2048:
                    self._children.popitem(last=False)

    def _claim(self, context):
        key = self._key(context)
        with self._lock:
            # Native child hooks and pre-API rotation preserve one parent claim;
            # arbitrary tool arguments cannot populate this map.
            match = self._claims.get(key)
            if match is None:
                matches = [value for scope, value in self._claims.items()
                           if scope[1:] == key[1:] and all(key[1:])]
                match = matches[0] if matches and all(value == matches[0] for value in matches) else None
            if match is None and key[0] in self._children:
                match = self._children[key[0]]
            return dict(match) if match else None

    def bind_turn(self, **context):
        # Native start/pre-API hooks attach inherited tokens to the child turn,
        # so compression or parent detachment cannot drop its fencing check.
        current = self._claim(context)
        if current is not None and all(self._key(context)):
            with self._lock:
                self._claims[self._key(context)] = current
                while len(self._claims) > 2048:
                    self._claims.popitem(last=False)

    def _request(self, commitment_id, payload):
        encoded = quote(commitment_id, safe='')
        response = self.client.post(f"/v1/host/commitments/{encoded}/work",
                                    json=payload, timeout=1)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get('commitment_id') != commitment_id or type(result.get('accepted')) is not bool:
            raise ValueError('invalid work response')
        return result

    def handle(self, args, scope, context):
        if (scope is None or not scope.valid_participant or not all(self._key(context))
                or set(args) != {'operation', 'commitment_id'}
                or args.get('operation') not in {'claim', 'status', 'release'}
                or not isinstance(args.get('commitment_id'), str)
                or not 1 <= len(args['commitment_id']) <= 256):
            return json.dumps({'error': 'Exact participant and commitment operation are required'})
        current = self._claim(context)
        if args['operation'] == 'claim' and current and current['commitment_id'] != args['commitment_id']:
            return json.dumps({'error': 'Release this turn\'s current undertaking before claiming another',
                               'commitment_id': current['commitment_id']})
        payload = {key: str(context.get(key) or '') for key in ('session_id', 'task_id', 'turn_id')}
        payload.update(contact_id=scope.contact_id, operation=args['operation'])
        if args['operation'] == 'release' and current:
            payload = {**current['holder'], 'operation': 'release', 'claim_id': current['claim_id']}
        try:
            result = self._request(args['commitment_id'], payload)
            if args['operation'] == 'claim' and result['accepted']:
                token = result.get('claim_id')
                if not isinstance(token, str) or len(token) != 32:
                    raise ValueError('missing undertaking token')
                with self._lock:
                    self._claims[self._key(context)] = {'commitment_id': args['commitment_id'],
                        'claim_id': token, 'holder': {key: value for key, value in payload.items() if key != 'operation'},
                        **({'child_session_id': current['child_session_id']} if current and current.get('child_session_id') else {})}
                    while len(self._claims) > 2048:
                        self._claims.popitem(last=False)
            elif (args['operation'] == 'release' and current
                    and args['commitment_id'] == current['commitment_id']
                    and (result['accepted'] or result.get('reason') in {'obligation_closed', 'claim_superseded'})):
                # An explicit stop may detach this turn after an authoritative
                # terminal/stale response; it never releases the new holder.
                # Children keep their snapshots and must stop independently.
                with self._lock:
                    for key, value in list(self._claims.items()):
                        if key[1:] == self._key(context)[1:] and value['claim_id'] == current['claim_id']:
                            self._claims.pop(key, None)
                    self._children.pop(current.get('child_session_id', self._key(context)[0]), None)
                result['detached'] = True
            # Tokens stay in the adapter; the model sees only work state.
            result.pop('claim_id', None)
            return json.dumps(result)
        except Exception:
            return json.dumps({'error': 'Commitment coordination unavailable; no undertaking confirmed'})

    def before_tool(self, context):
        if context.get('tool_name') == 'colony_commitment_work':
            return None
        current = self._claim(context)
        if current is None:
            return None
        try:
            result = self._request(current['commitment_id'], {**current['holder'],
                'operation': 'renew', 'claim_id': current['claim_id']})
            if result['accepted'] and result.get('claim_id') == current['claim_id']:
                return None
        except Exception:
            pass
        return json.dumps({'error': 'This undertaking is unavailable or superseded; stop its tools and inspect colony_commitment_work status',
                           'effect_performed': False, 'commitment_id': current['commitment_id']})
