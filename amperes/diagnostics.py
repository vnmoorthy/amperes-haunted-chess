"""Optional diagnostic summaries.

Default: deterministic template summaries that separate observed facts from possible causes. No model is called.
A model provider can be plugged in only by (1) registering a provider in code and (2) setting both
AMPERES_DIAGNOSTIC_PROVIDER=<name> and AMPERES_ALLOW_EXTERNAL_MODEL=1. No provider ships with Amperes, so in this
build no record ever leaves the machine. Model output is stored as a hypothesis, never as a finding or outcome.
"""

from __future__ import annotations

import os
from typing import Any, Callable

PROMPT_VERSION = "incident-summary/0"
PROVIDERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def status() -> dict[str, Any]:
    name = os.environ.get("AMPERES_DIAGNOSTIC_PROVIDER")
    allowed = os.environ.get("AMPERES_ALLOW_EXTERNAL_MODEL") == "1"
    enabled = bool(name and allowed and name in PROVIDERS)
    return {"enabled": enabled, "provider": name if enabled else None, "registered_providers": sorted(PROVIDERS),
            "data_flow": "no data leaves this machine" if not enabled else
            f"incident evidence (facts only, no raw streams) is sent to provider {name!r}"}


def summarize(incident: dict[str, Any]) -> dict[str, Any]:
    out = {"observed_facts": incident.get("facts", []),
           "possible_causes": [h["text"] for h in incident.get("hypotheses", [])],
           "evidence_needed": [h["evidence_needed"] for h in incident.get("hypotheses", [])],
           "generated_by": "deterministic template", "model": None, "prompt_version": None}
    st = status()
    if st["enabled"]:
        payload = {"facts": incident.get("facts"), "kind": incident.get("kind"), "category": incident.get("category"),
                   "n_occurrences": incident.get("n_occurrences"), "n_sessions": incident.get("n_sessions")}
        res = PROVIDERS[st["provider"]](payload)
        out["model_hypothesis"] = {"text": res.get("text"), "model": res.get("model"), "prompt_version": PROMPT_VERSION,
                                   "input": payload, "cost": res.get("cost"), "status": "unverified hypothesis"}
    return out
