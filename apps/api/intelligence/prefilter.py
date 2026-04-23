from __future__ import annotations

import re

# Tier 0 pre-filter. Before we spend tokens on an LLM classification call, we
# check whether the batch of utterances contains any lexical hint of a
# business signal. This catches the ~70% of batches that are pure filler
# ("uh", "yeah", "can you hear me"), which would otherwise burn Haiku tokens
# to return an empty signals array.
#
# Patterns are intentionally recall-over-precision: better to call the LLM
# and get an empty signals result than to silently drop a real signal. The
# LLM is still the ground truth for what actually makes it into the DB.

_SIGNAL_PATTERNS = [
    # commitments / next steps
    r"\b(by|before)\s+(mon|tue|wed|thu|fri|sat|sun)(day)?\b",
    r"\beod\b|\bend of (day|week|month|quarter|year)\b",
    r"\b(next week|this week|this month|next month|this quarter|next quarter)\b",
    r"\b(send|share|schedule|deliver|push|forward|ship)\b",
    r"\b(follow[- ]?up|followup|circle back|loop back)\b",
    r"\b(deal|contract|proposal|quote|signed|signed off|agree(d|ment)?|sow)\b",
    # competitors
    r"\b(gong|chorus|fathom|otter|fireflies|sybill|grain|dialpad)\b",
    # objections / price / risk
    r"\b(price|pricing|cost|expensive|cheaper|discount|budget|afford)\b",
    r"\b(concern|worry|worried|hesitant|issue|problem|blocker|tough sell)\b",
    r"\b(cancel|churn|leave|switch|competitor|eval(uating)?)\b",
    # urgency
    r"\b(urgent(ly)?|asap|critical|immediately|right now|right away)\b",
    # feature request
    r"\b(need|want|require|require?s?|must have|wish|would love|feature|integration)\b",
    r"\b(salesforce|hubspot|slack|jira|zendesk|segment|snowflake)\b",
    # customer goals
    r"\b(trying to|hoping to|looking for|goal is|goal here|aim is|we need to)\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _SIGNAL_PATTERNS]


def likely_contains_signal(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _COMPILED)


def batch_likely_has_signal(texts: list[str]) -> bool:
    """Return True if any utterance text in the batch matches a lexical signal
    hint. This is a *recall-biased* filter — false positives are fine, false
    negatives cost us a missed signal."""
    for t in texts:
        if likely_contains_signal(t):
            return True
    return False
