"""Replay/verification module.

Replays an hourly_plan against the (effectively-adjusted) scenario and
directives to confirm every constraint holds.  Returns a list of
(very concise) error strings; an empty list means the plan is valid.
"""
from __future__ import annotations

from typing import List


def apply_directives(hours, directives) -> dict:
    """Compute effective per-hour solar and per-hour reserve/cap overrides."""
    n = len(hours)
    eff_solar = [h["solar_kwh"] for h in hours]
    min_reserve_hours = [0.0] * n
    max_grid_hours = [float("inf")] * n
    no_charge = set()
    no_discharge = set()
    for d in directives:
        if not d or not d.get("applies"):
            continue
        dtype = d["directive_type"]
        adj = d.get("structured_adjustment") or {}
        hrs = adj.get("hours", [])
        if dtype == "solar_reduction":
            fac = adj.get("factor", 1.0)
            for h in hrs:
                eff_solar[h] *= fac
        elif dtype == "minimum_battery_reserve":
            for h in hrs:
                min_reserve_hours[h] = max(min_reserve_hours[h], adj.get("minimum_energy_kwh", 0))
        elif dtype == "max_grid_window":
            for h in hrs:
                max_grid_hours[h] = min(max_grid_hours[h], adj.get("max_grid_kwh", float("inf")))
        elif dtype == "no_charge_window":
            no_charge.update(hrs)
        elif dtype == "no_discharge_window":
            no_discharge.update(hrs)
    return {
        "eff_solar": eff_solar,
        "min_reserve": min_reserve_hours,
        "max_grid": max_grid_hours,
        "no_charge": no_charge,
        "no_discharge": no_discharge,
    }


def replay(hours, battery, plan, directives) -> List[str]:
    errs = []
    initial = float(battery["initial_energy_kwh"])
    capacity = float(battery["capacity_kwh"])
    base_min = float(battery["minimum_energy_kwh"])
    rate_c = float(battery["max_charge_kwh_per_hour"])
    rate_d = float(battery["max_discharge_kwh_per_hour"])

    params = apply_directives(hours, directives)
    eff_solar = params["eff_solar"]
    min_res = params["min_reserve"]
    max_g = params["max_grid"]
    no_c = params["no_charge"]
    no_d = params["no_discharge"]

    if len(plan) != 24:
        errs.append(f"plan has {len(plan)} entries instead of 24")
        return errs

    energy = initial
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0
    seen = set()

    for entry in plan:
        h = entry["hour"]
        if h in seen or h < 0 or h > 23:
            errs.append(f"duplicate or invalid hour {h}")
        seen.add(h)

        demand = hours[h]["demand_kwh"]
        solar = entry["solar_used_kwh"]
        grid = entry["grid_kwh"]
        bat = entry["battery_kwh"]
        action = entry["battery_action"]
        after = entry["battery_energy_after_kwh"]

        if solar < -0.01:
            errs.append(f"h{h}: solar_used_kwh < 0")
        if grid < -0.01:
            errs.append(f"h{h}: grid_kwh < 0")
        if solar > eff_solar[h] + 0.01:
            errs.append(f"h{h}: solar_used {solar} > effective {eff_solar[h]:.2f}")

        if action not in ("charge", "discharge", "idle"):
            errs.append(f"h{h}: bad action {action}")

        lo = max(base_min, min_res[h])
        if after < lo - 0.01:
            errs.append(f"h{h}: battery_after {after} < min {lo:.2f}")
        if after > capacity + 0.01:
            errs.append(f"h{h}: battery_after {after} > capacity {capacity}")

        expected_after = energy
        if action == "charge":
            if bat < -0.01 or bat > rate_c + 0.01:
                errs.append(f"h{h}: charge {bat} out of [0,{rate_c}]")
            if h in no_c and bat > 0.01:
                errs.append(f"h{h}: charge during no_charge_window")
            expected_after += bat
        elif action == "discharge":
            if bat < -0.01 or bat > rate_d + 0.01:
                errs.append(f"h{h}: discharge {bat} out of [0,{rate_d}]")
            if h in no_d and bat > 0.01:
                errs.append(f"h{h}: discharge during no_discharge_window")
            expected_after -= bat
        else:
            if bat > 0.01:
                errs.append(f"h{h}: idle but battery_kwh > 0")

        if action == "charge":
            exp = bat
        elif action == "discharge":
            exp = -bat
        else:
            exp = 0.0
        expected_energy = energy + exp
        if abs(after - expected_energy) > 0.01:
            errs.append(f"h{h}: battery_after {after} != expected {expected_energy:.4f}")

        balance = grid + solar + (bat if action == "discharge" else 0) - (bat if action == "charge" else 0)
        if abs(balance - demand) > 0.01:
            errs.append(f"h{h}: balance {balance:.4f} != demand {demand:.4f}")

        if grid > max_g[h] + 0.01:
            errs.append(f"h{h}: grid {grid} > cap {max_g[h]:.2f}")

        total_grid += grid
        total_cost += grid * hours[h]["tariff_bdt_per_kwh"]
        peak_grid = max(peak_grid, grid)
        energy = after

    if abs(energy - initial) > 0.01:
        errs.append(f"end-of-day battery {energy} != initial {initial}")

    if abs(total_grid - sum(e["grid_kwh"] for e in plan)) > 0.1:
        errs.append("total_grid_kwh mismatch")
    if abs(total_cost - sum(e["grid_kwh"] * hours[e["hour"]]["tariff_bdt_per_kwh"] for e in plan)) > 0.1:
        errs.append("total_cost_bdt mismatch")
    return errs