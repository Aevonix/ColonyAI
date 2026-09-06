"""Bounded observations of configured endpoints, separate from capabilities.

A model listing can omit a working request alias. Only completion outcomes
change routing availability; advertised metadata never grants tools or vision.
"""
import asyncio
from collections import OrderedDict
import hashlib
import threading
import time


def models_url(base_url):
    base = base_url.rstrip('/')
    return base + ('/models' if base.endswith('/v1') else '/v1/models')


class EndpointRuntime:
    def __init__(self, *, cooldown=15, ttl=30, clock=time.monotonic, wall=time.time):
        self.cooldown, self.ttl, self.clock, self.wall = cooldown, ttl, clock, wall
        self._calls, self._listings = OrderedDict(), OrderedDict()
        self._probing = set()
        self._lock = threading.RLock()

    @staticmethod
    def call_key(snapshot, binding):
        return (snapshot.revision, binding.config.base_url, binding.config.model_id, binding.weight_revision)

    @staticmethod
    def listing_key(snapshot, binding):
        credential = hashlib.sha256(binding.config.api_key.encode()).hexdigest()
        return (snapshot.revision, binding.config.base_url, credential)

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > 256:
            cache.popitem(last=False)

    def acquire(self, snapshot, binding, request_id):
        """Healthy calls are unrestricted; one call tests an expired cooldown."""
        key = self.call_key(snapshot, binding)
        with self._lock:
            state = self._calls.get(key)
            if not state or not state.get('failed'):
                return True
            if state['retry_at'] > self.clock() or state.get('recovering'):
                return False
            state['recovering'] = request_id
            return True

    def release(self, snapshot, binding, request_id):
        # Cancellation must not leave the single recovery attempt occupied.
        with self._lock:
            state = self._calls.get(self.call_key(snapshot, binding))
            if state and state.get('recovering') == request_id:
                state['recovering'] = False

    def failure(self, snapshot, binding, error):
        with self._lock:
            self._put(self._calls, self.call_key(snapshot, binding), {
                'failed': True, 'retry_at': self.clock() + self.cooldown,
                'checked': self.clock(),
                'observed_at': self.wall(), 'error_type': type(error).__name__,
                'status_code': getattr(error, 'status_code', None), 'recovering': False})

    def success(self, snapshot, binding, response):
        served = getattr(response.raw, 'model', None)
        with self._lock:
            self._put(self._calls, self.call_key(snapshot, binding), {
                'failed': False, 'observed_at': self.wall(),
                'checked': self.clock(),
                'served_model': served[:256] if isinstance(served, str) else None,
                'latency_ms': response.latency_ms})

    async def refresh(self, snapshot, probe):
        """At most four concurrent reads, only when /models is requested.

        Later reads cover remaining endpoints. No queue, scan or background task
        survives the request. Cached entries retain their actual observation age.
        """
        selected = []
        with self._lock:
            # Unknown and oldest observations go first, so infrequent reads
            # still cover a pool larger than the four concurrent probe slots.
            ordered = sorted(snapshot.bindings.values(), key=lambda binding:
                self._listings.get(self.listing_key(snapshot, binding), {}).get('checked', float('-inf')))
            for binding in ordered:
                key = self.listing_key(snapshot, binding)
                old = self._listings.get(key)
                if key in self._probing or (old and self.clock() - old['checked'] < self.ttl):
                    continue
                if len(self._probing) >= 4:
                    break
                self._probing.add(key)
                selected.append((key, binding))

        async def observe(key, binding):
            try:
                models = await asyncio.wait_for(probe(snapshot, binding), timeout=2)
                value = {'available': True, 'models': models, 'observed_at': self.wall()}
            except Exception as error:
                value = {'available': False, 'models': [], 'observed_at': self.wall(),
                         'error_type': type(error).__name__}
            else:
                value['error_type'] = None
            finally:
                with self._lock:
                    self._probing.discard(key)
            value['checked'] = self.clock()
            with self._lock:
                self._put(self._listings, key, value)

        await asyncio.gather(*(observe(key, binding) for key, binding in selected))

    def status(self, snapshot):
        now, wall = self.clock(), self.wall()
        bindings, inventories = {}, {}
        with self._lock:
            for name, binding in snapshot.bindings.items():
                state = self._calls.get(self.call_key(snapshot, binding))
                if state:
                    failed = state.get('failed')
                    phase = ('recovering' if state.get('recovering') else
                             'cooldown' if state.get('retry_at', 0) > now else 'retry_due') if failed else 'available'
                    bindings[name] = {k: v for k, v in state.items() if k not in {'failed', 'retry_at', 'recovering', 'checked'}}
                    bindings[name].update(state=phase,
                        stale=now-state['checked'] >= self.ttl,
                        age_seconds=round(max(0, wall-state['observed_at']), 1),
                        retry_after_seconds=round(max(0, state.get('retry_at', 0)-now), 2))
                else:
                    bindings[name] = {'state': 'unknown', 'stale': True}
                key = self.listing_key(snapshot, binding)
                if key not in inventories:
                    listing = self._listings.get(key)
                    item = {'bindings': [], 'available': False, 'models': [], 'stale': True}
                    if listing:
                        item.update({k: v for k, v in listing.items() if k != 'checked'})
                        item['age_seconds'] = round(max(0, wall-listing['observed_at']), 1)
                        item['stale'] = now-listing['checked'] >= self.ttl
                    inventories[key] = item
                inventories[key]['bindings'].append(name)
        return {'completion_observations': bindings, 'model_inventory': list(inventories.values()),
                'inventory_complete': all(not row['stale'] for row in inventories.values()),
                'observation_ttl_seconds': self.ttl, 'retry_cooldown_seconds': self.cooldown,
                'observation_basis': 'endpoint advertisements and actual completion outcomes; declarations remain authoritative'}
