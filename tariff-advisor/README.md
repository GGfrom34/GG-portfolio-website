# Octopus Tariff Advisor

An **informational aid for exploring Octopus Energy tariffs**. Given a UK household profile (region, solar panels, EV, home battery, average usage), it pulls current public rates from the Octopus Energy API and explains which tariffs look like the best fit and why.

> **This is not financial advice and not regulated switching advice.** It is an independent project, not affiliated with or endorsed by Octopus Energy. Every cost figure is an estimate built on the stated assumptions (a typical demand shape, an assumed solar yield, and so on) and will differ from a real household's bills. Always check current prices and terms on [octopus.energy](https://octopus.energy) before making any decision about switching.

## How it is organised

The code is split into core logic and an interface, so the same logic can sit behind more than one front end:

| File | Role |
|------|------|
| `core.py` | All the logic. Calls the Octopus API, models the household, ranks tariffs. It has no knowledge of the command line or of any other interface: it does no printing, prompting or exiting. The single entry point is `recommend_tariff(profile: dict) -> dict`, which returns plain, JSON-serialisable data. |
| `cli.py` | A thin command-line wrapper. It parses arguments into a profile dict, calls `core.recommend_tariff()`, and prints the result readably. It is the only file that knows about the command line. |
| `mcp_server.py` | A thin [MCP](https://modelcontextprotocol.io) server: a second interface over the same `recommend_tariff()`. It maps typed tool arguments to a profile dict, calls the core, and turns core errors into MCP tool errors. It contains no tariff logic. |
| `web_server.py` | A third interface: a small FastAPI backend that drives the Anthropic Messages API's tool-use loop directly against `core.py`, so a visitor to the public portfolio site can chat with the advisor without any MCP client. See [Web chat](#web-chat-public-site). |

Inside `core.py` the API-calling code is kept apart from the recommendation logic. The API layer fetches rates and returns normalised `Tariff` / `Market` objects. The recommendation engine works on those objects with no network access, so it can be tested offline by passing a hand-built `Market` to `recommend_tariff(profile, market=...)`. All three interfaces reuse the core unchanged, because it returns structured data rather than formatted text. Two things every model-driven interface needs are defined once in `core.py` rather than copied per adapter: `AGENT_INSTRUCTIONS` (the "answer first, then offer to refine" behavioral contract, wrapped in each adapter's own persona/framing) and `MarketCache` (a short TTL cache over `fetch_market`, since one recommendation costs about a dozen HTTP requests and a conversation tends to ask several times).

## Requirements

- **Core and CLI:** Python 3.9 or later, standard library only. No API key is needed, because only Octopus's public product and rate endpoints are used.
- **MCP server:** Python 3.10 or later and the `mcp` package (v2), installed from `requirements-mcp.txt` (see [MCP server](#mcp-server)). The core and CLI never import it.
- **Web chat backend:** Python 3.10 or later, `fastapi`/`uvicorn`/`anthropic`, installed from `requirements-web.txt` (see [Web chat](#web-chat-public-site)), and an `ANTHROPIC_API_KEY`. The core, CLI and MCP server never import it.

## Usage

Region is required and is the electricity distribution region letter (A-P); for example C is London and N is Southern Scotland. Run the tool without `--region` to see the full list of letters and names.

```bash
# No solar, EV or battery
python cli.py --region C --usage-kwh 3100

# Solar panels plus an EV charged overnight
python cli.py --region C --usage-kwh 4500 --solar --export-kw 4 --ev --ev-pattern overnight

# Solar, home battery that can charge off-peak, and an EV
python cli.py --region H --usage-kwh 5000 --solar --export-kw 4 --battery --battery-kwh 10 --battery-shifts --ev --ev-pattern overnight

# Use a profile file (flags override it), or print the raw result as JSON
python cli.py --config profile.example.json
python cli.py --config profile.example.json --json
```

| Flag | Meaning |
|------|---------|
| `--region` | GSP region letter A-P. **Required.** |
| `--usage-kwh` | Annual electricity use in kWh, **including EV charging**. Required. |
| `--solar`, `--export-kw` | Has solar panels; array size in kWp if known (defaults to 4 kWp). |
| `--ev`, `--ev-pattern`, `--ev-kwh-per-week` | Has an EV; `overnight` (off-peak only), `mixed` (a mix of off-peak and peak), `daytime` or `flexible`; rough weekly charging energy (defaults to 40 kWh). |
| `--battery`, `--battery-kwh`, `--battery-shifts` | Has a home battery; its capacity; whether it can charge at off-peak times and discharge at peak. |
| `--json` | Print the full result as JSON. |

### Using the core directly

```python
from core import recommend_tariff

result = recommend_tariff({
    "region": "C",
    "average_usage_kwh": 4500,
    "has_solar": True,
    "solar_kwp": 4,
    "has_ev": True,
    "ev_charging_pattern": "overnight",   # or "mixed", "daytime", "flexible"
    "ev_annual_kwh": 2500,                # or "ev_kwh_per_week"
})
print(result["summary"])
```

Bad input raises `ProfileError`; network problems raise `OctopusApiError`. The calling interface decides how to present them.

The core is **strict by default**: `region`, `average_usage_kwh`, `has_solar` and `has_ev` are required. Pass `allow_assumptions=True` and nothing is required: every missing input is filled with a stated assumption and reported in the result (see [First answer, then refine](#first-answer-then-refine)). The CLI stays strict; the MCP server opts in.

## MCP server

`mcp_server.py` lets an MCP client (Claude Desktop, Claude Code, or any other) use the advisor as tools. It runs over **stdio**: the client launches it as a local subprocess, so nothing is exposed on the network and there is no authentication to manage.

| Tool | What it does |
|------|--------------|
| `recommend_tariff` | The main tool. Takes the household profile as typed arguments and returns the ranked recommendations, reasons, caveats and assumptions. **Every argument is optional**: it answers from whatever it is given and reports what it assumed. |
| `find_region` | Turns a full UK postcode into the electricity region letter that `recommend_tariff` needs, using Octopus's public grid-supply-points lookup. If a postcode straddles two regions it says so instead of guessing. |
| `list_regions` | Lists the region letters and names. Works offline. |

All three tools are read-only. A few behaviours worth knowing:

- **Errors keep their message.** Bad input (for example an unknown region or a negative usage) comes back as a tool error that names the field, so the model can correct itself and retry.
- **Validation happens before any network call**, so invalid input never triggers an API request.
- **Rates are cached for 10 minutes per region** in the server (not in the core), because one recommendation makes about a dozen API requests and a conversation tends to ask several times.
- **Privacy:** a postcode is sent to Octopus only when `find_region` is called. It is not logged or stored.
- The server instructions and tool descriptions repeat the "informational, not financial or regulated switching advice" framing so the client relays it.

### First answer, then refine

The server tells the model (through its `instructions`, repeated in the `recommend_tariff` description) to behave like this:

1. **Answer first.** After the user's first message it calls `recommend_tariff` straight away, even when inputs are missing. It passes only what the user actually said and never invents values.
2. **State the assumptions** the answer rests on.
3. **End with an offer to refine:** a short line inviting the user to replace the assumptions, then a bullet list of *every* input that was missing. Later replies re-run the recommendation with everything known so far and list only what is still assumed. Once nothing is assumed, no offer is made.

The facts behind step 3 come from the tool result rather than the model's memory, so the list cannot be forgotten or invented. Each result carries `based_on_assumptions` and `assumed_inputs`: a list of `{input, question, assumed}` in a fixed order. For a first message like "I have an EV and solar panels" it is:

- the postcode of the property *(assumed: London, region C)*
- the total annual usage, including annual EV charging consumption *(assumed: about 2,500 kWh a year for a typical home plus 2,080 kWh a year of EV charging)*
- the size of the solar array (kWp) *(assumed: a 4 kWp array)*
- the annual EV charging consumption (kWh) *(assumed: about 2,080 kWh a year)*
- the EV charging pattern: off-peak only, or a mix of off-peak and peak *(assumed: mixed, 70% off-peak and 30% peak)*
- whether the home has battery storage *(assumed: no home battery)*

Defaults used when an input is missing (all listed in the result's `assumptions`): region London (C); about 2,500 kWh a year for a home before any EV (a round "typical household" figure, not a quoted Ofgem value); no solar, EV or battery unless stated; EV charging split 70% off-peak and 30% in the evening peak. A follow-up question appears only when it is relevant: the solar size is asked only if there is solar, the battery size only if there is a battery, and so on. Answers "no" to yes/no questions count as answers, not assumptions.

This relies on the model following the instructions. The list itself is data and is reliably correct, but whether a given client presents the closing offer is up to that client's model.

### Install and run

From this folder (a virtual environment keeps the MCP dependencies away from the standard-library-only core):

```bash
python -m venv .venv
# Windows:      .venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
# macOS/Linux:  .venv/bin/python -m pip install -r requirements-mcp.txt
```

Then start it (a client normally does this for you; running it by hand just waits for MCP messages on stdin):

```bash
# Windows:      .venv\Scripts\python.exe mcp_server.py
# macOS/Linux:  .venv/bin/python mcp_server.py
```

### Register it with a client

Use **absolute paths** to the virtual environment's Python and to `mcp_server.py`, since clients launch the server from their own working directory.

Claude Code:

```bash
claude mcp add octopus-tariff-advisor -- /path/to/tariff-advisor/.venv/bin/python /path/to/tariff-advisor/mcp_server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "octopus-tariff-advisor": {
      "command": "/path/to/tariff-advisor/.venv/bin/python",
      "args": ["/path/to/tariff-advisor/mcp_server.py"]
    }
  }
}
```

On Windows use `.venv\\Scripts\\python.exe` (backslashes doubled in JSON). Then ask something like: *"I'm at SW1A 1AA with solar panels and an EV that charges overnight, using about 4,000 kWh a year. Which Octopus tariffs should I look at?"*

## Web chat (public site)

`web_server.py` is a small FastAPI backend that lets a visitor to the portfolio site chat with the advisor directly, without an MCP client. It drives the Anthropic Messages API's tool-use loop itself against a `recommend_tariff` / `find_region` tool pair backed by `core.py`, and streams the reply to the browser as Server-Sent Events. The frontend is a plain-JS widget (`case-studies/tariff-advisor-widget.js`) embedded in the case-study page; there is no build step.

It carries its own persona on top of the same `core.AGENT_INSTRUCTIONS` behavioral contract the MCP server uses (see [First answer, then refine](#first-answer-then-refine)), so the underlying behavior is identical across both interfaces even though the voice differs.

**This is a public, unauthenticated endpoint, so it is deliberately mean with money.** Guardrails, all env-overridable:

| Guardrail | Default | Purpose |
|---|---|---|
| `GLOBAL_DAILY_TOKEN_BUDGET` | 10,000 tokens/day | A hard ceiling across every visitor combined. Persisted to a small local JSON file so a restart or redeploy mid-day doesn't reopen the budget. |
| `PER_IP_DAILY_TOKEN_BUDGET` | 2,000 tokens/day | Stops one visitor from consuming the whole global budget. Same persisted mechanism. |
| `PER_IP_RATE_LIMIT_PER_MIN` | 5 messages/minute | A burst limiter, separate from the token budgets above. |
| `MAX_INPUT_CHARS` | 1,000 characters | Per message. |
| `MAX_TURNS_PER_SESSION` | 12 tool-loop turns | Caps how long one conversation can run. |
| `MAX_TOKENS_PER_CALL` | 900 | The `max_tokens` cap passed to each Anthropic call. (Originally 400; too tight to fit a full recommendation plus the assumed_inputs refine offer, so responses got cut off mid-sentence and replaying a truncated turn broke follow-up questions.) |

Both token budgets are checked *before* every Anthropic call (using `max_tokens` as the reserved estimate) and reconciled against the actual `usage.input_tokens + usage.output_tokens` afterwards. Once either is exhausted, `/chat` returns a plain, non-technical message rather than an error, since running out is expected by design on a demo budget, not a fault. Sessions themselves are an in-memory, single-instance store (capped, oldest evicted first) — fine for a demo-scale service, but it means conversations and the rate limiter (though not the persisted token budgets) are lost on restart and are not shared across multiple instances.

### Install and run

```bash
python -m venv .venv   # or reuse the one from requirements-mcp.txt
# Windows:      .venv\Scripts\python.exe -m pip install -r requirements-web.txt
# macOS/Linux:  .venv/bin/python -m pip install -r requirements-web.txt
```

```bash
export ANTHROPIC_API_KEY=sk-...          # Windows: set ANTHROPIC_API_KEY=sk-...
export ALLOWED_ORIGIN=http://localhost:3000   # the origin the widget is served from
uvicorn web_server:app --reload
```

### Deploying

Any host that runs a persistent Python process works (so the in-memory session store, market cache, and rate limiter behave as intended between requests) — Render, Fly.io and Railway are all straightforward fits for a small FastAPI app. A `Procfile` is included:

```
web: uvicorn web_server:app --host 0.0.0.0 --port $PORT
```

Set `ANTHROPIC_API_KEY` and `ALLOWED_ORIGIN` (the portfolio site's real origin) on the host, then point `BACKEND_URL` at the top of `case-studies/tariff-advisor-widget.js` at the deployed URL. The persisted token-budget file (`.budget_state.json` by default, overridable via `BUDGET_STATE_PATH`) needs to live on a writable, persistent disk — on a host with an ephemeral filesystem, set `BUDGET_STATE_PATH` to a mounted volume, otherwise the budget silently resets on every restart.

**This project's own deployment** (`render.yaml`) uses Render's free web-service plan, which has no persistent disk *and* spins the instance down after ~15 minutes idle — its ephemeral filesystem is wiped on every spin-down, which would otherwise let anyone bypass the daily token budget just by waiting out an idle window between bursts. `.github/workflows/keep-tariff-advisor-warm.yml` pings `/health` every 10 minutes to keep the instance from ever idling out, closing that in practice. It does not protect against Render's own occasional maintenance restarts, so the free-tier deployment is meaningfully safer than an unmitigated one but still short of the hard guarantee a persistent disk (paid plan) or an externally-persisted store (e.g. Postgres) would give.

## Tests

Four offline suites (standard-library `unittest`, no network, no real Anthropic calls):

| File | What it tests |
|------|---------------|
| `test_core.py` | The recommendation engine against a hand-built `Market`: archetype classification, ranking, Agile demotion, battery shifting, export handling, profile validation, UK time/BST handling, rate parsing, and a check that `core.py` stays free of CLI code. |
| `test_api.py` | The API layer against **saved real API responses**, with `core._get_json` replaced by a playback stub: product discovery and filtering, per-region tariff lookup, the `varying` payment-method key on Flexible Octopus, rate windows, pagination, retry and error handling, and end-to-end recommendations on recorded rates. |
| `test_mcp_server.py` | The MCP adapter through an in-memory MCP client, with the recorded API responses behind it: the tool list and schemas (no required inputs), results matching the core exactly, the answer-first flow (a vague prompt still gets an answer and the assumed-inputs list shrinks as inputs arrive), error mapping, the rate cache (including concurrent callers), `find_region`, a real stdio subprocess handshake that also checks the instructions reach the client, and checks that the core never imports MCP and the adapter never prints or holds tariff logic. Skipped automatically when `mcp` is not installed. |
| `test_web_server.py` | The web chat adapter with a **fake Anthropic client** (canned tool_use/text content, so no tokens are spent) and the recorded API responses behind `core.py`: the tool loop and profile mapping, error-result mapping, that any exception (not just `anthropic.APIError`) ends the stream gracefully instead of crashing it, the rate limiter, the persisted daily token budget (global and per-IP, including surviving a simulated restart and resetting on a new UTC day), CORS, and a check that the core never imports FastAPI/Anthropic. Skipped automatically when `fastapi`/`anthropic` are not installed. |

```bash
python -m unittest -v test_core test_api                                   # standard library only
.venv/bin/python -m unittest -v test_core test_api test_mcp_server test_web_server   # everything installed
```

When you change the recommendation logic, add or adjust a test first. The engine tests assert on relationships (which tariff wins, what gets flagged), not on live prices, so they do not break when Octopus changes its rates.

### Saved API responses

`api_fixtures.py` holds the recorder and the playback stub; the recording lives in `fixtures/api_responses.json` (about 250 KB: regions C and N at a fixed time, plus one postcode lookup; 20 responses). The recorder captures exactly what `core.py` requests, so playback fails loudly if `core.py` starts requesting something that was not recorded, and a test fails if a recorded response is no longer used.

Re-record when `core.py`'s API requests change (new product family, different rate window), or to pick up a new Octopus response shape:

```bash
python api_fixtures.py
```

This needs network access. Expect some `test_api.py` assertions to need updating afterwards, because real prices and product codes will have changed; the fixed timestamp keeps the request windows reproducible.

## What it compares

Current products are discovered from the API each run, because Octopus product codes are dated and change over time.

- **Standard variable**: Flexible Octopus
- **Time-of-use import**: Agile Octopus (half-hourly), Octopus Go, Cosy Octopus, Intelligent Octopus Go
- **Export**: Outgoing Octopus and Agile Outgoing Octopus

Intelligent Octopus Go currently ships only as a 12-month fixed-term product (see its caveat in the
recommendation output); other fixed-term products are not compared.

## How the recommendation works

The profile is classified into one of eight archetypes (no solar/EV/battery; solar only; EV overnight; EV daytime or flexible; solar + EV; solar + battery; solar + battery + EV; battery only). For each import tariff the engine simulates an average day: it places EV charging, generates solar, runs the battery, and prices what is left to buy from the grid. Import tariffs are ranked by estimated annual cost and export tariffs by estimated income, and each result lists the rate that applies, why it suits the profile, and caveats. Agile is not ranked first unless the profile can shift load (flexible EV charging or a battery that charges off-peak), because its price spikes cannot otherwise be avoided.

The assumptions used are returned with every result under `assumptions`.

## Result shape

`recommend_tariff()` returns a dict with `archetype`, `summary`, `recommendations` (top import and, for solar households, top export), `alternatives`, `notes`, `warnings`, `assumptions`, `data_source` and `disclaimer`. Each recommendation carries `rates`, `reasons` and `caveats` as data, so any interface can present them its own way.
