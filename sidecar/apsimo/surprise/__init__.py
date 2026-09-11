"""Surprise Engine — anomaly detection when observations deviate from patterns."""

from apsimo.surprise.store import SurpriseStore
from apsimo.surprise.scorer import compute_surprise

__all__ = ["SurpriseStore", "compute_surprise"]
