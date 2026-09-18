# GridWise — Smart Campus Energy Optimizer
### BUP CSE Fest 2026 · Preliminary · LLM-Assisted Operator Directive Interpretation

Thank you for reviewing our submission. This README is written so anyone — including a judge on a
fresh machine — can run and understand the whole thing in a few minutes.

---

## 1. What does this service do? (60-second summary)

A campus buys electricity, has rooftop solar panels, and a battery. We are told:

- how much power the campus needs each hour (24 hours),
- how much solar will be available,
- what the grid charges per hour, and
- a few **operator notes** written in plain English, like
  *"Solar output will be reduced by 80% from 1 PM to 3 PM"*.

Our service does three things:

1. **Understands** each operator note (with an LLM) and converts it into a fixed, machine-checkable
   *directive* — e.g. `solar_reduction` on hours `[13,14]` with `factor: 0.2`.
2. **Verifies** those directives with strict deterministic guardrails before trusting them.
3. **Optimizes** a 24-hour schedule — how much to buy from the grid, use solar, and charge/discharge
   the battery — that follows every rule AND has the lowest possible grid cost.

It exposes exactly two endpoints for the judge:
`GET /health` and `POST /optimize-energy`.

---

## 2. How the code is organized

```
app/
  main.py         FastAPI app — /health and /optimize-energy endpoints
  schemas.py      Request/response models (Pydantic)
  interpreter.py  LLM operator-note understanding (+ safe deterministic fallback)
  guardrails.py   Validates and normalizes LLM output before it is trusted
  optimizer.py    Linear-program solver (HiGHS via scipy) + plan builder
  replay.py       Replays the plan hour-by-hour to prove it is valid
test_samples.py   Runs all 10 public sample cases against the pipeline (or a live server)
run.sh            One-command launcher for the API
Dockerfile        Containerizes the service for the Docker fallback
requirements.txt  Python dependencies
.env.example      Template for the environment variables (no secrets inside)
```

---

## 3. The pipeline: LLM → Guardrails → Optimizer

```
  POST /optimize-energy
        │  request JSON (24 hours + battery + 1–3 operator notes)
        ▼
┌────────────────────┐        ┌──────────────────────────────┐
│   interpreter.py   │  note  │  Gemini LLM (or any OpenAI-   │
│   builds the prompt│ ─────► │  compatible endpoint)         │
└────────────────────┘        └──────────────────────────────┘
        │  <- structured directives (treated as UNTRUSTED)
        ▼
┌────────────────────┐
│   guardrails.py    │  "no_op" must be applies=false, hours must be
│   deterministic    │  unique 0–23 ascending, factor 0–1, reserve <=
│   validation       │  capacity... anything invalid is rejected
└────────────────────┘
        ▼  clean directives
┌────────────────────┐        ┌──────────────────────────────┐
│   optimizer.py     │  LP +  │  HiGHS solver (scipy.optimize │
│   builds 24h plan  │  →     │  .linprog) — exact optimum    │
└────────────────────┘        └──────────────────────────────┘
        ▼  hourly_plan (24 entries)
┌────────────────────┐
│   replay.py        │  independent re-check: energy balance, battery
│   prove it is valid│  state/limits, solar ceiling, end-of-day return
└────────────────────┘
        ▼
   response JSON (plan + interpretation + totals + summary)
```

**The three key ideas the judges asked about:**

- **LLM role (mandatory).** The language model genuinely *reads the operator notes* and produces the
  structured `directive_interpretation` that the optimizer then uses. It is not used just to write a
  pretty summary — it sits directly in the interpretation path.
- **Guardrails.** LLM output is untrusted. Before any directive touches the optimizer, a
  deterministic validator checks type, hour range/order, numeric bounds, and `applies` semantics.
  Anything malformed is rejected rather than guessed.
- **Optimizer.** The scheduling problem is a small linear program (120 variables, 48 constraints),
  solved to an exact optimum by HiGHS in milliseconds.

---

## 4. What you need (dependencies)

- Python 3.10+
- The packages in `requirements.txt`:
  `fastapi`, `uvicorn`, `scipy`, `numpy`, `requests`, `python-dotenv`

That's it — no database, no external broker, everything is self-contained.

---

## 5. Model / provider used

| Setting   | Value |
|-----------|-------|
| Provider  | Google Gemini (OpenAI-compatible endpoint) |
| Model     | `gemini-3.5-flash` |
| Endpoint  | `https://generativelanguage.googleapis.com/v1beta/openai/` |

Because the service uses an OpenAI-compatible chat-completions interface, the same code works with
any OpenAI-compatible provider — OpenAI, DeepSeek, Groq, OpenRouter, or a local Ollama server.

---

## 6. Environment variables

Set these before starting the service. Credentials live only in the environment / `.env` file —
never in the repository or image.

| Variable         | What it is                                   | Default |
|------------------|----------------------------------------------|---------|
| `LLM_API_KEY`    | API key for the LLM endpoint                 | *(none → deterministic fallback)* |
| `LLM_BASE_URL`   | OpenAI-compatible base URL                   | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_MODEL`      | Model identifier                             | `gemini-3.5-flash` |
| `LLM_TIMEOUT_S`  | Timeout for one LLM call                     | `20` |
| `PORT`           | HTTP port (used by `run.sh`)                 | `8000` |

The service also reads a `.env` file automatically (via `python-dotenv`), so you can copy
`.env.example` → `.env` and fill in `LLM_API_KEY`.

---

## 7. Run it locally (copy-paste quickstart)

From a clean machine:

```bash
git clone <your-repo-url>
cd <repo-folder>

# 1) create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) provide the LLM key (optional but recommended)
cp .env.example .env
# edit .env and set LLM_API_KEY=<your Gemini or OpenAI-compatible key>

# 3) start the service
./run.sh
# -> uvicorn listening on http://0.0.0.0:8000
```

> No `LLM_API_KEY`? No problem. The service falls back to its built-in deterministic interpreter,
> so you can still run and test everything locally.

---

## 8. Test it

### Health check

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### One optimization request (curl)

```bash
curl -s -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  -d '{
        "scenario_id": "GRID-101",
        "operator_notes": [
          "Solar output will drop to about 20% from 1 PM to 3 PM.",
          "Do not charge the battery between 2 PM and 4 PM.",
          "The cafeteria menu changes tomorrow."
        ],
        "hours": [
          {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
          {"hour": 1, "demand_kwh": 170, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
          {"hour": 2, "demand_kwh": 160, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
          {"hour": 3, "demand_kwh": 155, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
          {"hour": 4, "demand_kwh": 160, "solar_kwh": 0, "tariff_bdt_per_kwh": 5},
          {"hour": 5, "demand_kwh": 170, "solar_kwh": 0, "tariff_bdt_per_kwh": 6},
          {"hour": 6, "demand_kwh": 185, "solar_kwh": 5, "tariff_bdt_per_kwh": 8},
          {"hour": 7, "demand_kwh": 205, "solar_kwh": 20, "tariff_bdt_per_kwh": 10},
          {"hour": 8, "demand_kwh": 235, "solar_kwh": 50, "tariff_bdt_per_kwh": 12},
          {"hour": 9, "demand_kwh": 260, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
          {"hour": 10, "demand_kwh": 275, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
          {"hour": 11, "demand_kwh": 280, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
          {"hour": 12, "demand_kwh": 285, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
          {"hour": 13, "demand_kwh": 280, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
          {"hour": 14, "demand_kwh": 270, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
          {"hour": 15, "demand_kwh": 265, "solar_kwh": 90, "tariff_bdt_per_kwh": 14},
          {"hour": 16, "demand_kwh": 275, "solar_kwh": 45, "tariff_bdt_per_kwh": 18},
          {"hour": 17, "demand_kwh": 295, "solar_kwh": 10, "tariff_bdt_per_kwh": 22},
          {"hour": 18, "demand_kwh": 320, "solar_kwh": 0, "tariff_bdt_per_kwh": 28},
          {"hour": 19, "demand_kwh": 330, "solar_kwh": 0, "tariff_bdt_per_kwh": 30},
          {"hour": 20, "demand_kwh": 320, "solar_kwh": 0, "tariff_bdt_per_kwh": 26},
          {"hour": 21, "demand_kwh": 290, "solar_kwh": 0, "tariff_bdt_per_kwh": 18},
          {"hour": 22, "demand_kwh": 250, "solar_kwh": 0, "tariff_bdt_per_kwh": 10},
          {"hour": 23, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 9}
        ],
        "battery": {
          "capacity_kwh": 500,
          "initial_energy_kwh": 200,
          "minimum_energy_kwh": 50,
          "max_charge_kwh_per_hour": 100,
          "max_discharge_kwh_per_hour": 100
        }
      }'
```

The response contains `directive_interpretation` (3 entries), `hourly_plan` (24 entries), plus
`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, and `plan_summary`.

### Run the full public-sample suite

```bash
python3 test_samples.py                       # function-level (no server needed)
python3 test_samples.py --http http://localhost:8000   # against the running server
```

**Expected result: `10/10 cases fully correct`.** The script checks three things per case:

1. the LLM/fallback interpretation matches the ground truth,
2. the returned schedule is fully valid when replayed (energy balance, battery rules, directives),
3. the cost equals the optimal reference cost (tolerance 0.01).

It is normal for the exact per-hour schedule to differ from the reference — the challenge explicitly
accepts any *equivalent optimal* schedule; only validity and cost matter.

---

## 9. Docker (judge fallback path)

```bash
docker build -t gridwise .
docker run -d --name gridwise -p 8000:8000 \
  -e LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/ \
  -e LLM_MODEL=gemini-3.5-flash \
  -e LLM_API_KEY=<your-key-here> \
  gridwise

curl http://localhost:8000/health          # -> {"status":"ok"}
```

- Exposed port: `8000`
- Binds to `0.0.0.0` (reachable from outside)
- No secrets baked into the image — the key is passed at runtime via `-e`

---

## 10. What the service guarantees (validation)

Every plan is replayed before it leaves the server. The judge does not have to trust us — they can
replay too. Guarantees:

- exactly 24 unique hours `0–23`
- per-hour energy balance: `grid + solar_used + battery_discharge = demand + battery_charge`
- battery energy always within `[minimum reserve, capacity]`, honoring any reserve directive
- hourly charge/discharge never exceeds the rate limits
- `no_charge` / `no_discharge` windows, and `max_grid` caps, are strictly respected
- sun is never over-used: `solar_used_kwh ≤ effective solar` (after any reduction)
- the battery ends the day exactly where it started (end-of-day neutrality)
- totals are recomputed from the plan, so `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`
  always agree with `hourly_plan`
- exactly one `directive_interpretation` per note, in `note_index` order; `no_op` uses
  `applies=false` and `structured_adjustment=null`

Malformed JSON or structurally invalid requests get a clean `422`. Internal failures return a
controlled `500` with no stack traces or secrets.

---

## 11. Supported directives (quick reference)

| `directive_type` | Meaning | `structured_adjustment` |
|---|---|---|
| `solar_reduction` | usable solar reduced | `{hours, factor}` (factor = fraction remaining; 80% cut ⇒ 0.2) |
| `minimum_battery_reserve` | keep battery above a level | `{hours, minimum_energy_kwh}` |
| `no_charge_window` | no charging in those hours | `{hours}` |
| `no_discharge_window` | no discharging in those hours | `{hours}` |
| `max_grid_window` | grid import cap per hour | `{hours, max_grid_kwh}` |
| `no_op` | note does not affect today | `null` |

Time windows are start-inclusive / end-exclusive: "1 PM to 3 PM" ⇒ `[13, 14]`.

---

## 12. Known limitations (honest note)

- The built-in fallback interpreter recognizes the directive vocabulary covered by the public
  samples and common paraphrases; it may miss an unusual wording that the primary LLM path handles.
  The fallback only activates when no LLM key is present or when the LLM output is invalid.
- Our schedules are equivalent-optimal, not byte-identical to the reference plans — as the
  challenge permits.

---

## 13. Security & repository policy

- **No secrets are committed.** All credentials are supplied at runtime through environment
  variables. `.env`, `.venv`, and log files are git-ignored.
- Repository is **private during the event** and will be made **public after the submission
  deadline**, as the rulebook requires.
- The sample data comes only from the official synthetic case pack.

---

*Built for BUP CSE Fest 2026 · Preliminary · Smart Campus Energy Optimization Challenge.*