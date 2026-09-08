"""Compatibility for retired composite closeness; historical contact values remain.

Standing, frequency, recency and a contact's affect are not one relationship
quality. New behavior uses attributed preferences and scoped AppraisalStore views.
"""
from typing import Any, Optional


def compute_relationship_score(contact: Any, affect_state: Optional[dict] = None, now=None) -> None:
    """No meaningful scalar is computed. Do not write this into contact storage."""
    return None


def closeness_label(score) -> str:
    """Old stored values carry no supported psychological interpretation."""
    return 'legacy score; relationship interpretation unavailable'
