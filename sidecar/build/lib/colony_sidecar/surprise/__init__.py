"""Surprise Engine — anomaly detection when observations deviate from patterns."""

from colony_sidecar.surprise.store import SurpriseStore
from colony_sidecar.surprise.scorer import compute_surprise

__all__ = ["SurpriseStore", "compute_surprise"]
