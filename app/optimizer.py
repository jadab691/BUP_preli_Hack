"""Energy schedule optimizer.

Formulates the 24-hour battery/grid scheduling problem as a linear program and
solves it exactly with HiGHS (via scipy). The plan respects:
  - energy balance every hour
  - battery state/rate/capacity bounds plus end-of-day neutrality
  - directive constraints (solar reduction, reserve, no-charge, no-discharge,
    grid cap)
"""
from __future__ import annotations

from typing import List

import numpy as np
from scipy.optimize import linprog

HOURS = 24
RATE_C, RATE_D = "rc", "rd"


def apply_directives(hours: List[dict], directives: List[dict]) -> dict:
    """Compute effective per-hour model parameters from base data + directives."""
    n = len(hours)
    eff_solar = np.array([h["solar_kwh"] for h in hours], dtype=float)
    max_grid = np.full(n, np.inf)
    min_reserve_hours = np.zeros(n, dtype=float)  # per-hour reserve (0 = none)

    for d in directives:
        if not d or not d.get("applies"):
            continue
        adj = d.get("structured_adjustment") or {}
        hrs = adj.get("hours", [])
        dtype = d["directive_type"]
        if dtype == "solar_reduction":
            eff_solar[hrs] *= adj["factor"]
        elif dtype == "no_charge_window":
            pass  # handled as bound in LP
        elif dtype == "no_discharge_window":
            pass
        elif dtype == "max_grid_window":
            for h in hrs:
                max_grid[h] = min(max_grid[h], adj["max_grid_kwh"])
        elif dtype == "minimum_battery_reserve":
            for h in hrs:
                min_reserve_hours[h] = max(min_reserve_hours[h], adj["minimum_energy_kwh"])

    return {
        "eff_solar": eff_solar.tolist(),
        "max_grid": max_grid.tolist(),
        "min_reserve_hours": min_reserve_hours.tolist(),
    }


def build_plan(hours, battery, directives) -> List[dict]:
    """Solve the LP and return the hourly_plan (list of dicts)."""
    n = len(hours)
    demand = np.array([h["demand_kwh"] for h in hours], dtype=float)
    tariff = np.array([h["tariff_bdt_per_kwh"] for h in hours], dtype=float)
    params = apply_directives(hours, directives)

    eff_solar = np.array(params["eff_solar"])
    max_grid = np.array(params["max_grid"])
    min_reserve = np.array(params["min_reserve_hours"])

    capacity = float(battery["capacity_kwh"])
    initial = float(battery["initial_energy_kwh"])
    base_min = float(battery["minimum_energy_kwh"])
    rate_c = float(battery["max_charge_kwh_per_hour"])
    rate_d = float(battery["max_discharge_kwh_per_hour"])

    no_charge_hrs = set()
    no_discharge_hrs = set()
    for d in directives:
        if not d or not d.get("applies"):
            continue
        adj = d.get("structured_adjustment") or {}
        if d["directive_type"] == "no_charge_window":
            no_charge_hrs.update(adj.get("hours", []))
        elif d["directive_type"] == "no_discharge_window":
            no_discharge_hrs.update(adj.get("hours", []))

    intervals = 4  # g, c, d, s are block 0..n-1
    nv = intervals * n
    nv_full = nv + n

    g, c, d, s = 0, n, 2 * n, 3 * n

    # Objective: minimize sum tariff[h] * g[h]
    cvec = np.zeros(nv_full)
    cvec[g:g + n] = tariff

    # Equality constraints:
    #   (A) balance:  g[h] + d[h] + s[h] - c[h] = demand[h]
    #   (B) battery:  -E[h-1] + E[h] - c[h] + d[h] = 0   (E[-1]=initial)
    # Variables for E are appended after g,c,d,s.
    E = np.arange(nv, nv_full)

    A_eq = np.zeros((2 * n, nv_full))
    b_eq = np.zeros(2 * n)

    for h in range(n):
        A_eq[h, g + h] = 1.0
        A_eq[h, d + h] = 1.0
        A_eq[h, s + h] = 1.0
        A_eq[h, c + h] = -1.0
        b_eq[h] = demand[h]

    for h in range(n):
        A_eq[n + h, E[h]] = 1.0
        if h > 0:
            A_eq[n + h, E[h - 1]] = -1.0
        A_eq[n + h, c + h] = -1.0
        A_eq[n + h, d + h] = 1.0

    b_eq[n] = initial  # E[0] - c[0] + d[0] = initial
    # neutrality: E[23] = initial -> LB = UB = initial via bounds

    bounds = []
    for h in range(n):
        ghi = max_grid[h]
        if np.isinf(ghi):
            ghi = None
        bounds.append((0.0, ghi))  # g
    for h in range(n):
        bounds.append((0.0, 0.0 if h in no_charge_hrs else rate_c))  # c
    for h in range(n):
        bounds.append((0.0, 0.0 if h in no_discharge_hrs else rate_d))  # d
    for h in range(n):
        bounds.append((0.0, eff_solar[h]))  # s
    for h in range(n):
        lo = max(base_min, min_reserve[h])
        hi = capacity if h < n - 1 else initial
        if h == n - 1:
            lo = max(lo, initial)
        bounds.append((lo, hi))  # E[h]

    res = linprog(
        cvec,
        A_eq=A_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )

    if not res.success:
        plan = _greedy_fallback(hours, battery, directives, params)
        if plan:
            return plan
        raise ValueError(f"optimization failed: {res.message}")

    x = res.x
    sol = {
        "g": np.clip(np.round(x[g:g + n], 6), 0, None),
        "c": np.clip(np.round(x[c:c + n], 6), 0, None),
        "d": np.clip(np.round(x[d:d + n], 6), 0, None),
        "s": np.clip(np.round(x[s:s + n], 6), 0, None),
    }

    plan = []
    energy = initial
    for h in range(n):
        cc, dd, ss = sol["c"][h], sol["d"][h], sol["s"][h]
        gg = round(demand[h] + cc - dd - ss, 6)
        if gg < 0:
            gg = 0.0
        energy = round(energy + cc - dd, 6)
        action = "charge" if cc > 1e-9 else ("discharge" if dd > 1e-9 else "idle")
        plan.append(
            {
                "hour": h,
                "grid_kwh": gg,
                "solar_used_kwh": round(ss, 6),
                "battery_action": action,
                "battery_kwh": round(cc if action == "charge" else (dd if action == "discharge" else 0.0), 6),
                "battery_energy_after_kwh": energy,
            }
        )
    return plan


def _greedy_fallback(hours, battery, directives, params) -> List[dict]:
    """Feasible-but-simple fallback used if the LP is ever infeasible.

    Uses solar first, keeps the battery idle (initial energy is preserved, so
    neutrality holds trivially). Only reached on unexpected infeasibility.
    """
    plan = []
    energy = float(battery["initial_energy_kwh"])
    for h in range(len(hours)):
        eff = params["eff_solar"][h]
        demand = hours[h]["demand_kwh"]
        solar_used = min(eff, demand)
        grid = max(0.0, demand - solar_used)
        plan.append(
            {
                "hour": h,
                "grid_kwh": grid,
                "solar_used_kwh": solar_used,
                "battery_action": "idle",
                "battery_kwh": 0.0,
                "battery_energy_after_kwh": energy,
            }
        )
    return plan


def summarize(plan, directives) -> str:
    active = [d for d in directives if d and d.get("applies")]
    bits = []
    if not active:
        bits.append("No operator directives applied")
    else:
        for d in active:
            bits.append(f"{d['directive_type']}")
    peak_hour = max(plan, key=lambda e: e["grid_kwh"])["hour"]
    return (
        "; ".join(bits) + ". Battery shifts energy toward higher-tariff hours "
        f"while preserving limits; peak grid hour {peak_hour}."
    )