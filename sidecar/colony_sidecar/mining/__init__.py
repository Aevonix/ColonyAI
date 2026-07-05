"""Mining: escalation detection + training-corpus export.

The model-agnostic mining half of a continuous self-improvement loop:
escalation events (build-agent consultations, heavy-model / cloud-failover
turns) become skills-memory distillation inputs and golden eval cases, and
the verbatim turn capture becomes fine-tune-ready JSONL for an external
training pipeline. Everything stays under COLONY_STATE_DIR.
"""
from colony_sidecar.mining.corpus import export_corpus
from colony_sidecar.mining.escalations import EscalationMiner
from colony_sidecar.mining.models import (
    EscalationRecord,
    MinedTurn,
    corpus_export_enabled,
    mining_mode,
)
from colony_sidecar.mining.store import MiningStore

__all__ = [
    "EscalationMiner",
    "EscalationRecord",
    "MinedTurn",
    "MiningStore",
    "corpus_export_enabled",
    "export_corpus",
    "mining_mode",
]
