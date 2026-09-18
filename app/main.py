"""GridWise HTTP API service.

Endpoints:
  GET  /health           -> {"status": "ok"}
  POST /optimize-energy  -> directive interpretation + optimized 24h schedule
"""
from __future__ import annotations

import logging
import sys
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.guardrails import validate_directives
from app.interpreter import interpret_notes
from app.optimizer import HOURS, build_plan, summarize
from app.replay import replay
from app.schemas import (
    DirectiveInterpretation,
    OptimizeRequest,
    OptimizeResponse,
    PlanEntry,
)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
log = logging.getLogger("gridwise")

app = FastAPI(title="GridWise", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/optimize-energy")
def optimize(req: OptimizeRequest) -> OptimizeResponse:
    battery = req.battery.model_dump()
    hours = [h.model_dump() for h in req.hours]
    preview = [hours[0], hours[1], hours[2], hours[-1]]

    try:
        scenario = interpret_notes(req.operator_notes, battery, preview)
        directives = validate_directives(scenario.directives, battery)
        plan = build_plan(hours, battery, directives)
    except Exception as exc:  # controlled failure, never leak internals
        log.error("optimize failed: %s\n%s", exc, traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": "internal optimization failure"},
        )

    errs = replay(hours, battery, plan, directives)
    if errs:
        log.warning("plan validation warnings: %s", "; ".join(errs[:5]))

    total_grid = round(sum(e["grid_kwh"] for e in plan), 6)
    total_cost = round(
        sum(e["grid_kwh"] * hours[e["hour"]]["tariff_bdt_per_kwh"] for e in plan), 6
    )
    peak_grid = max(e["grid_kwh"] for e in plan)

    return OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=[
            DirectiveInterpretation(**d) for d in directives
        ],
        hourly_plan=[PlanEntry(**e) for e in plan],
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=summarize(plan, directives),
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled: %s", traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "internal error"})