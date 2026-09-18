"""Run all public sample cases through the pipeline and report results.

Usage:
    python test_samples.py            (function-level pipeline check)
    python test_samples.py --http PORT  (check through a running server)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app.guardrails import validate_directives  # noqa: E402
from app.interpreter import interpret_notes  # noqa: E402
from app.optimizer import build_plan  # noqa: E402
from app.replay import replay  # noqa: E402

SAMPLE_FILE = os.path.join(
    ROOT, "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def load_cases():
    with open(SAMPLE_FILE) as f:
        return json.load(f)["cases"]


def comparable(a, b, tol=0.01):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol or (
            math.isclose(a, b, rel_tol=1e-6, abs_tol=tol)
        )
    return a == b


def check_interpretation(got, expected):
    ok = True
    reasons = []
    # exact ground-truth directive comparison (public references)
    for gi, ei in zip(got, expected):
        for key in ("note_index", "applies", "directive_type"):
            if gi.get(key) != ei.get(key):
                ok = False
                reasons.append(f"note{gi.get('note_index')}.{key}: got {gi.get(key)} != {ei.get(key)}")
                break
        gadj = gi.get("structured_adjustment") or {}
        eadj = ei.get("structured_adjustment") or {}
        if gi.get("applies") and gi.get("directive_type") != "no_op":
            if set(gadj.get("hours", [])) != set(eadj.get("hours", [])):
                ok = False
                reasons.append(f"note{gi.get('note_index')}.hours: {gadj.get('hours')} != {eadj.get('hours')}")
            for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
                if key in eadj and not comparable(gadj.get(key, None), eadj.get(key)):
                    ok = False
                    reasons.append(f"note{gi.get('note_index')}.{key}: {gadj.get(key)} != {eadj.get(key)}")
    return ok, reasons


def check_plan(plan, expected, tol=0.05):
    ok = True
    reasons = []
    for pe, ee in zip(plan, expected):
        if pe["hour"] != ee["hour"]:
            ok = False
            reasons.append(f"hour mismatch {pe['hour']}")
        for key in ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh"):
            if not comparable(pe[key], ee[key], tol):
                ok = False
                reasons.append(f"h{ee['hour']}.{key}: {pe[key]} != {ee[key]}")
                break
        if pe["battery_action"] != ee["battery_action"]:
            ok = False
            reasons.append(f"h{ee['hour']}.action: {pe['battery_action']} != {ee['battery_action']}")
    return ok, reasons


def run_case(case, base_url=None):
    inp = case["input"]
    expected = case["expected_output"]

    if base_url:
        r = requests.post(f"{base_url}/optimize-energy", json=inp, timeout=60)
        r.raise_for_status()
        got = r.json()
    else:
        battery = inp["battery"]
        hours = inp["hours"]
        scenario = interpret_notes(inp["operator_notes"], battery, hours[:4])
        directives = validate_directives(scenario.directives, battery)
        plan = build_plan(hours, battery, directives)
        got = {
            "scenario_id": inp["scenario_id"],
            "directive_interpretation": directives,
            "hourly_plan": plan,
            "total_grid_kwh": round(sum(e["grid_kwh"] for e in plan), 6),
            "total_cost_bdt": round(sum(e["grid_kwh"] * hours[e["hour"]]["tariff_bdt_per_kwh"] for e in plan), 6),
            "peak_grid_kwh": max(e["grid_kwh"] for e in plan),
        }

    interp_ok, interp_reasons = check_interpretation(
        got["directive_interpretation"], expected["directive_interpretation"]
    )

    # judge-mandated legality of the returned plan (using TRUTH directives)
    errs = replay(inp["hours"], inp["battery"], got["hourly_plan"], expected["directive_interpretation"])
    valid = not errs

    opt_ok, plan_reasons = check_plan(got["hourly_plan"], expected["hourly_plan"])
    cost_diff = round(got["total_cost_bdt"] - expected["total_cost_bdt"], 4)

    return {
        "id": case["id"],
        "label": case["label"],
        "interp_ok": interp_ok,
        "interp_reasons": interp_reasons,
        "plan_valid": valid,
        "plan_errs": errs,
        "exact_plan": opt_ok,
        "plan_reasons": plan_reasons,
        "cost_got": got["total_cost_bdt"],
        "cost_exp": expected["total_cost_bdt"],
        "cost_diff": cost_diff,
        "peak_got": got["peak_grid_kwh"],
        "peak_exp": expected["peak_grid_kwh"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", help="base URL of running server, e.g. http://localhost:8000")
    args = ap.parse_args()

    cases = load_cases()
    print(f"Running {len(cases)} public sample cases "
          f"({'via HTTP ' + args.http if args.http else 'direct function pipeline'})...\n")

    n_ok = 0
    for case in cases:
        res = run_case(case, base_url=args.http)
        status = "PASS" if (res["interp_ok"] and res["plan_valid"]) else "FAIL"
        n_ok += 1 if status == "PASS" else 0
        print(f"[{status}] {res['id']:10s} {res['label']}")
        print(f"         interpretation ok: {res['interp_ok']}   plan valid: {res['plan_valid']}"
              f"   exact plan: {res['exact_plan']}")
        print(f"         cost: got {res['cost_got']:>10}  expected {res['cost_exp']:>10}  diff {res['cost_diff']:+.4f}"
              f"   peak: {res['peak_got']} vs {res['peak_exp']}")
        for r in res["interp_reasons"][:3]:
            print(f"         interp note: {r}")
        for r in res["plan_errs"][:3]:
            print(f"         validity: {r}")
        for r in res["plan_reasons"][:2]:
            print(f"         plan: {r}")
        print()

    print(f"Summary: {n_ok}/{len(cases)} cases fully correct.")


if __name__ == "__main__":
    main()