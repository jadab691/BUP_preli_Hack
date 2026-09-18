"""Operator-note interpretation.

Primary path: a language-capable generative model (LLM) converts each operator
note into a structured directive. A deterministic rule-based fallback is used
only when no LLM credentials are configured or when the LLM output fails the
guardrails, so the service remains usable for offline development/testing.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()  # optional .env support for local development

# ---------------------------------------------------------------------------
# Matching words for percentage / fraction expressions
# ---------------------------------------------------------------------------

_FRAC_WORDS = {
    "one-fifth": 0.2,
    "one fifth": 0.2,
    "a fifth": 0.2,
    "1/5": 0.2,
    "a quarter": 0.25,
    "quarter": 0.25,
    "half": 0.5,
    "one-half": 0.5,
}

_HOUR_WORDS = {
    "midnight": 0,
    "noon": 12,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_DIGIT_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?(?:\s*([ap]\.?m\.?))?\b", re.I
)
_WORD_TIME_RE = re.compile(
    r"\b(noon|midnight|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve)(?:\s*([ap]\.?m\.?))?\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Directive:
    note_index: int
    applies: bool = False
    directive_type: str = "no_op"
    structured_adjustment: Optional[dict] = None
    explanation: str = ""


@dataclass
class InterpretedScenario:
    directives: List[Directive] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rule-based fallback interpreter (offline / no-LLM mode)
# ---------------------------------------------------------------------------

def _parse_clock(token, mer) -> Optional[int]:
    """Map a time token to an hour 0..23. token is digit str or word str."""
    if mer:
        mer = mer.replace(".", "").lower()[:1]
        if token.isdigit():
            h = int(token)
            if mer == "p" and h != 12:
                h += 12
            elif mer == "a" and h == 12:
                h = 0
            return h
        else:
            base = _HOUR_WORDS.get(token.lower())
            if base is None:
                return None
            if mer == "p" and base != 12:
                return base + 12
            if mer == "a" and base == 12:
                return 0
            return base

    if token.isdigit():
        return int(token)
    return _HOUR_WORDS.get(token.lower())


def extract_window_hours(text: str, assume_pm: bool = False) -> List[int]:
    """Extract the affected hours from a time window.

    Conventions: start-inclusive, end-exclusive. '1 PM to 3 PM' -> [13,14].
    Only time-like tokens are considered (numbers followed by nothing else are
    ignored unless they look like clock times with a meridian or a colon).
    """
    tokens: list[int] = []
    has_meridian = False

    # Capture ranges like "1-3 PM", "6-9 PM", "13-15" BEFORE digit tokenizer.
    for m in re.finditer(
        r"\b(\d{1,2})\s*[-\u2013]\s*(\d{1,2})\s*([ap]\.?m\.?)?\b", text, re.I
    ):
        a, b, mer = int(m.group(1)), int(m.group(2)), (m.group(3) or "").replace(".", "").lower()
        if a > 23 or b > 23:
            continue
        if mer.startswith("p"):
            ha = (a % 12) + 12
            hb = (b % 12) + 12
        elif mer.startswith("a"):
            ha = a % 12
            hb = b % 12
        else:
            ha, hb = a, b
        has_meridian = has_meridian or bool(mer)
        tokens.append((ha, m.start()))
        tokens.append((hb, m.end()))

    # Also handle "one-until-three" style via existing word ranges.
    for m in _DIGIT_TIME_RE.finditer(text):
        h = int(m.group(1))
        mer = (m.group(3) or "").replace(".", "").lower()
        minute = m.group(2)
        if h > 23:
            continue
        if minute is None and not mer:
            # bare number without am/pm and without minutes: only accept when
            # it is clearly a clock (followed/preceded by a window word later);
            # safest is to skip bare numbers entirely here.
            continue
        if mer.startswith("p"):
            h24 = (h % 12) + 12
        elif mer.startswith("a"):
            h24 = h % 12
        else:
            h24 = h  # 24h form, e.g. 13:00
        if mer:
            has_meridian = True
        tokens.append((h24, m.start()))

    for m in _WORD_TIME_RE.finditer(text):
        base = _HOUR_WORDS[m.group(1).lower()]
        mer = (m.group(2) or "").replace(".", "").lower()
        if mer.startswith("p"):
            h24 = 12 if base == 12 else base + 12
        elif mer.startswith("a"):
            h24 = base % 12
        else:
            h24 = base
        if mer:
            has_meridian = True
        tokens.append((h24, m.start()))

    if not tokens:
        return []

    # Keep tokens that look like a window start/end: exclude stray tokens far
    # from any window separator. Simplest robust choice: use min and max.
    vals = [t[0] for t in tokens]
    lo, hi = min(vals), max(vals)

    if assume_pm and not has_meridian and lo <= 12 and hi <= 12:
        lo += 12
        hi += 12

    lo = max(0, min(lo, 23))
    hi = max(lo + 1, min(hi, 24))
    if hi <= lo:
        return []
    return list(range(lo, hi))


def _extract_percent(text: str) -> Optional[float]:
    for phrase, val in _FRAC_WORDS.items():
        if phrase in text:
            return val
    m = re.search(r"(\d{1,3})\s*(?:%|percent\b)", text, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)) / 100.0


def _solar_factor(text: str, pct: float) -> float:
    """Determine the usable fraction from a percentage and wording.

    'drop to 20%' / 'falls to 20%' -> 0.2 (the value it goes TO).
    '80% reduction' / 'reduce by 30%' -> 1 - pct.
    'leaves 25% of forecast' / 'stays at 25%' -> pct (a remaining fraction).
    """
    if re.search(r"\b(drop|fall|down|decrease|lowers?)\b.*\bto\b", text, re.I):
        return pct
    if re.search(r"\breduc", text, re.I) or (
        re.search(r"\b(drops?|falls?|decreases?)\b", text, re.I)
        and re.search(r"\bby\b", text, re.I)
    ):
        return 1.0 - pct
    return pct


def interpret_note_fallback(note: str, index: int, battery) -> Directive:
    low = note.lower()

    # --- minimum_battery_reserve ---
    if re.search(
        r"\b(keep at least|requires? at least|must remain|must hold|reserve|emergency)\b",
        low,
    ):
        m = re.search(r"(\d+(?:\.\d+)?)\s*kwh?|(\d+(?:\.\d+)?)\s*kilowatt", low, re.I)
        pct = _extract_percent(low)
        min_kwh = None
        if pct is not None and re.search(r"capacity|battery|stored|remain|hold", low):
            min_kwh = pct * battery["capacity_kwh"]
        elif m:
            min_kwh = float(m.group(1) or m.group(2))
        if min_kwh is not None:
            hours = extract_window_hours(note)
            if hours:
                return Directive(
                    note_index=index,
                    applies=True,
                    directive_type="minimum_battery_reserve",
                    structured_adjustment={"hours": hours, "minimum_energy_kwh": round(min_kwh, 4)},
                    explanation="Battery is kept above a required reserve level during the stated window.",
                )

    # --- no_discharge_window ---
    if re.search(
        r"\b(not discharge|no discharging|no discharge|must not discharge|"
        r"don't discharge|cannot discharge|protect|relay testing)\b",
        low,
    ):
        hours = extract_window_hours(note)
        if hours:
            return Directive(
                note_index=index,
                applies=True,
                directive_type="no_discharge_window",
                structured_adjustment={"hours": hours},
                explanation="Battery discharging is unavailable during the stated window.",
            )

    # --- no_charge_window ---
    if re.search(r"\b(charge|charging|charger)\b", low) and re.search(
        r"\b(unavailable|disabled|not\s+charge|no charging|no charge|"
        r"charging circuit.*unavailable|will be isolated|outage|down "
        r"for maintenance|disabled|inspect)\b",
        low,
    ):
        hours = extract_window_hours(note)
        if hours:
            return Directive(
                note_index=index,
                applies=True,
                directive_type="no_charge_window",
                structured_adjustment={"hours": hours},
                explanation="Battery charging is unavailable during the stated window.",
            )

    # --- max_grid_window ---
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", low, re.I)
    if re.search(r"\b(exceed|over|capped|limit|cap|below|at or below|intake|constrain)\b", low) and re.search(
        r"\b(grid|import|intake|feeder|transformer|substation|demand)\b", low
    ):
        hours = extract_window_hours(note)
        if m and hours:
            return Directive(
                note_index=index,
                applies=True,
                directive_type="max_grid_window",
                structured_adjustment={"hours": hours, "max_grid_kwh": float(m.group(1))},
                explanation="Grid import is capped during the stated window.",
            )

    # --- solar_reduction ---
    if re.search(r"\b(solar|pv|rooftop|panel|inverter)\b", low) and re.search(
        r"clean|wash|reduc|drop|degrad|shade|cloud|maintenance|usable|output|inspect|cover", low
    ):
        hours = extract_window_hours(note, assume_pm=True)
        pct = _extract_percent(low)
        if pct is not None and hours:
            factor = round(_solar_factor(note, pct), 4)
            return Directive(
                note_index=index,
                applies=True,
                directive_type="solar_reduction",
                structured_adjustment={"hours": hours, "factor": factor},
                explanation="Usable solar availability is reduced during the stated window.",
            )

    # --- no_op ---
    return Directive(
        note_index=index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation="This note does not affect today's 24-hour energy schedule.",
    )


# ---------------------------------------------------------------------------
# LLM-based interpreter
# ---------------------------------------------------------------------------

def _llm_config() -> dict:
    raw = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    placeholder = "your-key" in raw.lower() or "paste_" in raw.lower() or "xxxx" in raw.lower()
    return {
        "api_key": None if placeholder else (raw or None),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        "timeout": float(os.environ.get("LLM_TIMEOUT_S", "20")),
    }


_DIRECTIVE_DOC = """\
You interpret campus operator notes into structured energy directives.

SUPPORTED DIRECTIVE TYPES (exactly one per note):
1. solar_reduction  -> {"hours":[int], "factor":float}   factor is the usable fraction remaining (0..1). An 80% reduction means factor=0.2.
2. minimum_battery_reserve -> {"hours":[int], "minimum_energy_kwh":float}
3. no_charge_window -> {"hours":[int]}
4. no_discharge_window -> {"hours":[int]}
5. max_grid_window  -> {"hours":[int], "max_grid_kwh":float}
6. no_op            -> applies=false, structured_adjustment=null

RULES:
- Time windows are start-inclusive and end-exclusive whole hours in 24h time:
  "1 PM to 3 PM" => hours [13, 14]. "noon until 2 PM" => [12, 13]. "6 PM until 9 PM" => [18, 19, 20].
- hours must be unique integers 0..23 in ASCENDING order.
- If a note does not affect today's 24-hour energy schedule, mark it no_op
  (applies=false). Never invent a directive type or numeric values not stated.
- Distractor notes (deadlines, menus, unrelated events) are no_op.
- For minimum_battery_reserve, convert relative language: "50% of the battery
  capacity" means 0.5 * capacity_kwh.
- "about"/"roughly"/"approx" percentages use that value directly.
- A note saying solar drops to X% means factor = X/100.
"""


def _llm_prompt(notes, hours_preview, battery) -> str:
    return f"""{_DIRECTIVE_DOC}

CURRENT SCENARIO:
battery = {json.dumps(battery)}
hours preview (first 3 and last 1) = {hours_preview}

OPERATOR NOTES:
{json.dumps(notes, ensure_ascii=False)}

Return ONLY a valid JSON array (no markdown fences, no commentary) with one
object per note in the same order:
[
 {{"note_index":0,"applies":true,"directive_type":"solar_reduction",
   "structured_adjustment":{{"hours":[12,13],"factor":0.25}},
   "explanation":"Short reason here."}},
 ...
]
Only use the directive types listed above."""


def _call_llm(notes, hours_preview, battery) -> Optional[List[dict]]:
    cfg = _llm_config()
    if not cfg["api_key"]:
        return None
    payload = {
        "model": cfg["model"],
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": "You convert operator notes to strict structured directives."},
            {"role": "user", "content": _llm_prompt(notes, hours_preview, battery)},
        ],
        "response_format": {"type": "json_object"},
    }
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

    import time as _time

    last_err: Optional[Exception] = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg['api_key']}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
                body = json.loads(resp.read().decode())
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            items = data.get("directives", data) if isinstance(data, dict) else data
            items = data["directives"] if isinstance(data, dict) and "directives" in data else (items if isinstance(items, list) else [data])
            if isinstance(items, dict):
                items = [items]
            return items if isinstance(items, list) else None
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 401, 403, 404):
                return None  # not transient; let the fallback handle it
        except Exception as e:  # network/timeout/json errors
            last_err = e
        if attempt < 2:
            _time.sleep(1.0 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# Top-level interpretation entry point
# ---------------------------------------------------------------------------

def interpret_notes(notes: list, battery: dict, hours_preview=None) -> InterpretedScenario:
    cfg = _llm_config()
    raw = None
    if cfg["api_key"]:
        try:
            attempt = _call_llm(notes, hours_preview, battery)
            if attempt:
                raw = attempt
        except Exception:  # never let an LLM failure crash the request
            raw = None

    directives: List[Directive] = []
    for i, note in enumerate(notes):
        d = None
        if raw and i < len(raw):
            d = _coerce_llm_item(raw[i], i, battery)
        if d is None:
            d = interpret_note_fallback(note, i, battery)
        directives.append(d)
    return InterpretedScenario(directives=directives)


def _coerce_llm_item(item, index: int, battery) -> Optional[Directive]:
    if not isinstance(item, dict):
        return None
    dtype = str(item.get("directive_type", "")).strip()
    adj = item.get("structured_adjustment")
    if dtype == "no_op" or not dtype:
        if dtype == "no_op":
            return Directive(
                note_index=index,
                applies=False,
                directive_type="no_op",
                structured_adjustment=None,
                explanation=str(item.get("explanation", "No-op.")),
            )
        return None
    if dtype not in {
        "solar_reduction", "minimum_battery_reserve", "no_charge_window",
        "no_discharge_window", "max_grid_window",
    }:
        return None
    if not isinstance(adj, dict):
        return None
    try:
        hours = sorted(set(int(h) for h in adj.get("hours", [])))
    except (TypeError, ValueError):
        return None
    if not hours or any(h < 0 or h > 23 for h in hours):
        return None
    sa = {"hours": hours}
    if dtype == "solar_reduction":
        factor = float(adj.get("factor", -1))
        if not 0.0 <= factor <= 1.0:
            return None
        sa["factor"] = factor
    elif dtype == "minimum_battery_reserve":
        val = float(adj.get("minimum_energy_kwh", -1))
        if val < 0 or val > battery["capacity_kwh"] + 1e-6:
            return None
        sa["minimum_energy_kwh"] = val
    elif dtype == "max_grid_window":
        val = float(adj.get("max_grid_kwh", -1))
        if val < 0:
            return None
        sa["max_grid_kwh"] = val
    return Directive(
        note_index=index,
        applies=True,
        directive_type=dtype,
        structured_adjustment=sa,
        explanation=str(item.get("explanation", "")),
    )