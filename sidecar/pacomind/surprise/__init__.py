"""Surprise Engine — anomaly detection when observations deviate from patterns."""

from pacomind.surprise.store import SurpriseStore
from pacomind.surprise.scorer import compute_surprise

__all__ = ["SurpriseStore", "compute_surprise"]
