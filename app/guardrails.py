"""Deterministic guardrails: validate/normalize interpreted directives before
they are allowed to influence the optimization model.

LLM output is treated as untrusted structured data until this module passes it.
"""
from __future__ import annotations

from typing import List

ALLOWED_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def validate_directives(directives, battery: dict) -> List[dict]:
    """Return a clean, normalized list of directive dicts.

    Each entry carries note_index, applies, directive_type,
    structured_adjustment, explanation. Invalid directives fall back to no_op
    (controlled failure) so the optimizer never sees malformed input.
    """
    out: List[dict] = []
    for d in directives:
        note_index = int(getattr(d, "note_index", 0))
        dtype = str(getattr(d, "directive_type", ""))
        adj = getattr(d, "structured_adjustment", None)
        explanation = str(getattr(d, "explanation", ""))

        if dtype not in ALLOWED_TYPES:
            out.append(_no_op(note_index))
            continue

        if dtype == "no_op":
            out.append(_no_op(note_index))
            continue

        if adj is None:
            out.append(_no_op(note_index))
            continue

        adj = dict(adj)
        hours = adj.get("hours")
        if not isinstance(hours, list) or not hours:
            out.append(_no_op(note_index))
            continue
        try:
            hours = sorted({int(h) for h in hours})
        except (TypeError, ValueError):
            out.append(_no_op(note_index))
            continue
        if any(h < 0 or h > 23 for h in hours):
            out.append(_no_op(note_index))
            continue

        clean = {"hours": hours}

        if dtype == "solar_reduction":
            try:
                factor = float(adj["factor"])
            except (KeyError, TypeError, ValueError):
                out.append(_no_op(note_index))
                continue
            if not 0.0 <= factor <= 1.0:
                out.append(_no_op(note_index))
                continue
            clean["factor"] = round(factor, 4)

        elif dtype == "minimum_battery_reserve":
            try:
                val = float(adj["minimum_energy_kwh"])
            except (KeyError, TypeError, ValueError):
                out.append(_no_op(note_index))
                continue
            cap = float(battery["capacity_kwh"])
            if val < 0 or val > cap + 1e-6:
                out.append(_no_op(note_index))
                continue
            clean["minimum_energy_kwh"] = round(val, 4)

        elif dtype == "max_grid_window":
            try:
                val = float(adj["max_grid_kwh"])
            except (KeyError, TypeError, ValueError):
                out.append(_no_op(note_index))
                continue
            if val < 0:
                out.append(_no_op(note_index))
                continue
            clean["max_grid_kwh"] = round(val, 4)

        out.append(
            {
                "note_index": note_index,
                "applies": True,
                "directive_type": dtype,
                "structured_adjustment": clean,
                "explanation": explanation,
            }
        )

    # Enforce strictly increasing, packed note_index order 0..N-1 and
    # re-normalize hours arrays.
    ordered = sorted(out, key=lambda x: x["note_index"])
    slist = []
    for i, d in enumerate(ordered):
        d["note_index"] = i
        if d["structured_adjustment"]:
            d["structured_adjustment"]["hours"] = sorted(
                set(int(h) for h in d["structured_adjustment"]["hours"])
            )
        slist.append(d)
    return slist


def _no_op(note_index: int) -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": "This note does not affect today's 24-hour energy schedule.",
    }