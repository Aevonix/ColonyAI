"""Qualified property observations in the existing world observation journal.

Stable entities describe what a thing is. This projection describes what was
observed, reported or hypothesized about it, when, and for which audience.
Confidence is deliberately absent: repetition is not independent evidence.
The host authenticates observation producers and resolves canonical references.
"""
from datetime import datetime, timezone
import json
import re

from apsimo.self_model.situation import _safe_id, _validate_scope

KINDS = ('observed', 'reported', 'hypothesis')
SOURCE_PREFIX = 'world-property-v1:'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def epoch(value):
    if isinstance(value, bool):
        raise ValueError('invalid_world_observation_time')
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('world_observation_time_requires_timezone')
    return parsed.timestamp()


def iso(value):
    return datetime.fromtimestamp(epoch(value), timezone.utc).isoformat()


def observation(*, observation_id, entity_id, property_key, value, kind, producer,
                evidence_refs, observed_at, fresh_until, subject_person_id,
                viewer_scope, shareability, valid_from=None, valid_to=None, source_refs=()):
    for key, item in [('observation_id', observation_id), ('entity_id', entity_id), ('producer', producer)]:
        _safe_id(item, key)
    if not isinstance(property_key, str) or not re.fullmatch('[a-z][a-z0-9_.-]{0,63}', property_key):
        raise ValueError('invalid_world_property_key')
    if kind not in KINDS:
        raise ValueError('invalid_world_observation_kind')
    if not isinstance(evidence_refs, (tuple, list)) or not 1 <= len(evidence_refs) <= 40:
        raise ValueError('world_observation_requires_evidence')
    refs = sorted({_safe_id(ref, 'evidence_ref') for ref in evidence_refs})
    if not isinstance(source_refs, (list, tuple)) or len(source_refs) > 40:
        raise ValueError('invalid_world_source_refs')
    sources = []
    for source in source_refs:
        if (not isinstance(source, dict) or set(source) != {'source_id', 'source_version'}
                or not isinstance(source['source_version'], str)
                or not re.fullmatch('[0-9a-f]{64}', source['source_version'])):
            raise ValueError('invalid_world_source_refs')
        _safe_id(source['source_id'], 'source_id')
        if 'source:' + source['source_id'] not in refs:
            raise ValueError('world_source_ref_requires_evidence')
        sources.append(dict(source))
    # Preserve existing source/receipt/media identifiers; do not invent text
    # claims or upload referenced media into this journal.
    subject, viewer, sharing = _validate_scope(subject_person_id, viewer_scope, shareability)
    observed, fresh = iso(observed_at), iso(fresh_until)
    start, end = iso(valid_from if valid_from is not None else observed_at), iso(valid_to) if valid_to is not None else None
    if epoch(fresh) < epoch(observed) or epoch(fresh) - epoch(observed) > 366 * 86400:
        raise ValueError('world_observation_freshness_must_be_bounded')
    if end is not None and epoch(end) <= epoch(start):
        raise ValueError('invalid_world_observation_validity')
    if len(canonical(value).encode()) > 8192:
        raise ValueError('world_observation_value_too_large')
    return dict(schema='WorldPropertyObservationV1', observation_id=observation_id,
        entity_id=entity_id, property_key=property_key, value=value, kind=kind, producer=producer,
        evidence_refs=refs, source_refs=sorted(sources, key=lambda s: s['source_id']),
        observed_at=observed, fresh_until=fresh, valid_from=start, valid_to=end,
        subject_person_id=subject, viewer_scope=viewer, shareability=sharing)


def project(records, *, entity_id, property_key, subject_person_id, viewer_scope,
            shareability, as_of=None, coverage_limited=False):
    """Observed-time view. Delayed arrival cannot win over a newer observation.

    Each producer's latest applicable report is considered separately. Fresh
    observations outrank reports, which outrank hypotheses, but disagreement
    remains explicit. A stale latest observation cannot revive its older value.
    ``as_of`` answers what held then using available evidence, not what the
    system had already ingested then.
    """
    _validate_scope(subject_person_id, viewer_scope, shareability)
    now = epoch(as_of if as_of is not None else datetime.now(timezone.utc).isoformat())
    visible = []
    for row in records:
        if (row.get('schema') != 'WorldPropertyObservationV1' or row.get('entity_id') != entity_id
                or row.get('property_key') != property_key):
            continue
        if (row['subject_person_id'], row['viewer_scope'], row['shareability']) != (subject_person_id, viewer_scope, shareability):
            continue
        if epoch(row['observed_at']) > now or epoch(row['valid_from']) > now:
            continue
        visible.append(row)
    # Repackaging one receipt under fresh IDs cannot renew freshness or create
    # independent corroboration. Keep its first observed occurrence per value.
    dedup = {}
    for row in sorted(visible, key=lambda r: (epoch(r['observed_at']), r['observation_id'])):
        key = (row['producer'], row['kind'], tuple(row['evidence_refs']), canonical(row['value']))
        dedup.setdefault(key, row)
    latest = {}
    for row in dedup.values():
        key = (row['producer'], row['kind'])
        moment = epoch(row['observed_at'])
        if key not in latest or moment > epoch(latest[key][0]['observed_at']):
            latest[key] = [row]
        elif moment == epoch(latest[key][0]['observed_at']):
            latest[key].append(row)
    candidates = [dict(row, freshness='invalidated' if row.get('invalidated') else 'fresh' if epoch(row['fresh_until']) > now and
        (row['valid_to'] is None or epoch(row['valid_to']) > now) else 'stale')
        for rows in latest.values() for row in rows]
    current = [row for row in candidates if row['freshness'] == 'fresh']
    best = next(([r for r in current if r['kind'] == kind] for kind in KINDS
                 if any(r['kind'] == kind for r in current)), [])
    values = {canonical(row['value']) for row in best}
    state = 'conflicted' if len(values) > 1 else 'current' if best else 'stale' if any(
        r['freshness'] == 'stale' for r in candidates) else 'unknown'
    if coverage_limited:
        state = 'unknown'
    selected = best if state == 'current' else []
    value = selected[0]['value'] if selected else None
    refs = sorted({ref for row in best for ref in row['evidence_refs']})
    return dict(schema='WorldPropertyStateV1', entity_id=entity_id, property_key=property_key,
        subject_person_id=subject_person_id, viewer_scope=viewer_scope, shareability=shareability,
        state=state, value=value, kind=selected[0]['kind'] if selected else None,
        as_of=datetime.fromtimestamp(now, timezone.utc).isoformat(), time_basis='observed_at',
        evidence_refs=refs, observations=sorted(candidates, key=lambda r: (r['producer'], r['kind'], r['observation_id'])),
        has_disagreement=len({canonical(r['value']) for r in current}) > 1,
        coverage_limited=coverage_limited, authority_granted=False)


def compact_situation(snapshot, *, limit=12):
    """Scoped present-tense facts for context; stale values never become current."""
    if snapshot is None:
        return dict(schema='WorldSituationViewV1', state='unknown', facts=[], stale=[], authority_granted=False)
    limit = max(1, min(int(limit), 32))
    def compact(fact):
        return {key: fact.public()[key] for key in ('observation_id', 'category', 'entity_id', 'state', 'active',
            'observed_at', 'fresh_until', 'freshness', 'evidence_refs', 'attributes')}
    facts = [compact(f) for f in snapshot.facts if f.freshness == 'fresh']
    stale = [dict(entity_id=f.entity_id, category=f.category, state='unknown', last_observed_state=f.state,
                  observed_at=f.observed_at, fresh_until=f.fresh_until, evidence_refs=list(f.evidence_refs))
             for f in snapshot.facts if f.freshness != 'fresh']
    return dict(schema='WorldSituationViewV1', snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of,
        subject_person_id=snapshot.subject_person_id, viewer_scope=snapshot.viewer_scope,
        shareability=snapshot.shareability, state='current' if facts else 'stale' if stale else 'unknown',
        facts=facts[:limit], stale=stale[:limit], omitted=max(0, len(facts)-limit)+max(0, len(stale)-limit),
        authority_granted=False)
