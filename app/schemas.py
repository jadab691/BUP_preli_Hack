from typing import List, Optional, Literal
from pydantic import BaseModel, field_validator


class Hour(BaseModel):
    hour: int
    demand_kwh: float
    solar_kwh: float
    tariff_bdt_per_kwh: float


class Battery(BaseModel):
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float


class OptimizeRequest(BaseModel):
    scenario_id: str
    operator_notes: List[str]
    hours: List[Hour]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def notes_between_1_and_3(cls, v):
        if not 1 <= len(v) <= 3:
            raise ValueError("operator_notes must contain 1 to 3 notes")
        return v

    @field_validator("hours")
    @classmethod
    def exactly_24_hours(cls, v):
        if len(v) != 24:
            raise ValueError("hours must contain exactly 24 entries")
        hours = sorted(h.hour for h in v)
        if hours != list(range(24)):
            raise ValueError("hours must be 0..23")
        return v


class SchedAdjustment(BaseModel):
    hours: Optional[List[int]] = None
    factor: Optional[float] = None
    minimum_energy_kwh: Optional[float] = None
    max_grid_kwh: Optional[float] = None


DIRECTIVE_TYPES = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BATTERY_ACTIONS = Literal["charge", "discharge", "idle"]


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DIRECTIVE_TYPES
    structured_adjustment: Optional[SchedAdjustment] = None
    explanation: str


class PlanEntry(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: BATTERY_ACTIONS
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[PlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str