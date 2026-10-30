"""Decide whether a prompt describes an incident (run the cross-layer timeline) or a plain question.

"What is causing application slowness" is an incident even when typed into the Ask box, while
"Which pods use more than 1 GB?" is a lookup. Rules, in order:
  1. an explicit cause/outage phrase                      -> incident
  2. survey / lookup phrasing ("which", "how many", ...)  -> question
  3. a symptom word (slow, failing, 5xx, crash ...)       -> incident
  4. otherwise                                            -> question
"""
from __future__ import annotations

import re
from typing import Literal

Kind = Literal["incident", "question"]

_CAUSE = re.compile(
    r"\b(why (is|are|does|do|did|has|have|was|were)\b|what('?s| is| was| are) (causing|wrong|behind|broken)|"
    r"root[- ]cause|caus(e|es|ing) (of|the)|(is|are|went|goes) down\b|not (working|responding|starting|loading)|"
    r"stopped working|outage|incident|can'?t (connect|reach|pay|log ?in|access)|unable to (connect|reach|access))",
    re.I)
_SURVEY = re.compile(
    r"^\s*(is anything|is there anything|are (any|there)|which|list|how many|show( me)?|count|give me|"
    r"what (was|is|were|got) (deployed|changed|running)|what changed|when (was|did))\b", re.I)
_SYMPTOM = re.compile(
    r"\b(slow(ness|er)?|latency|lagg?(ing|y)?|down|fail(s|ed|ing|ures?)?|broken|crash\w*|errors?|5\d\d|5xx|"
    r"time ?outs?|timing out|unavailable|unreachable|degraded|oom\w*|evicted|restarting|hang(s|ing)?|stuck|"
    r"pending|backoff|refused)\b", re.I)


def classify(prompt: str) -> Kind:
    text = (prompt or "").strip()
    if _CAUSE.search(text):
        return "incident"
    if _SURVEY.search(text):
        return "question"
    if _SYMPTOM.search(text):
        return "incident"
    return "question"
