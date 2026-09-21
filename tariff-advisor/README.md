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

Inside `core.py` the API-calling code is kept apart from the recommendation logic. The API layer fetches rates and returns normalised `Tariff` / `Market` objects. The recommendation engine works on those objects with no network access, so it can be tested offline by passing a hand-built `Market` to `recommend_tariff(profile, market=...)`. Both interfaces reuse the core unchanged, because it returns structured data rather than formatted text.

## Requirements

- **Core and CLI:** Python 3.9 or later, standard library only. No API key is needed, because only Octopus's public product and rate endpoints are used.
- **MCP server:** Python 3.10 or later and the `mcp` package (v2), installed from `requirements-mcp.txt` (see [MCP server](#mcp-server)). The core and CLI never import it.

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
| `--solar`, `--export-kw` | Has solar panels; array / export capacity in kW if known (defaults to 4 kW). |
| `--ev`, `--ev-pattern`, `--ev-kwh-per-week` | Has an EV; `overnight`, `daytime` or `flexible`; rough weekly charging energy (defaults to 40 kWh). |
| `--battery`, `--battery-kwh`, `--battery-shifts` | Has a home battery; its capacity; whether it can charge at off-peak times and discharge at peak. |
| `--json` | Print the full result as JSON. |

### Using the core directly

```python
from core import recommend_tariff

result = recommend_tariff({
    "region": "C",
    "average_usage_kwh": 4500,
    "has_solar": True,
    "export_capacity_kw": 4,
    "has_ev": True,
    "ev_charging_pattern": "overnight",
})
print(result["summary"])
```

Bad input raises `ProfileError`; network problems raise `OctopusApiError`. The calling interface decides how to present them.

## MCP server

`mcp_server.py` lets an MCP client (Claude Desktop, Claude Code, or any other) use the advisor as tools. It runs over **stdio**: the client launches it as a local subprocess, so nothing is exposed on the network and there is no authentication to manage.

| Tool | What it does |
|------|--------------|
| `recommend_tariff` | The main tool. Takes the household profile as typed arguments (`region`, `average_usage_kwh`, `has_solar`, `has_ev` are required; solar size, EV pattern and weekly kWh, and battery details are optional) and returns the ranked recommendations, reasons, caveats and assumptions. |
| `find_region` | Turns a full UK postcode into the electricity region letter that `recommend_tariff` needs, using Octopus's public grid-supply-points lookup. If a postcode straddles two regions it says so instead of guessing. |
| `list_regions` | Lists the region letters and names. Works offline. |

All three tools are read-only. A few behaviours worth knowing:

- **Errors keep their message.** Bad input (for example an EV with no charging pattern) comes back as a tool error that names the field, so the model can correct itself and retry.
- **Validation happens before any network call**, so invalid input never triggers an API request.
- **Rates are cached for 10 minutes per region** in the server (not in the core), because one recommendation makes about a dozen API requests and a conversation tends to ask several times.
- **Privacy:** a postcode is sent to Octopus only when `find_region` is called. It is not logged or stored.
- The server instructions and tool descriptions repeat the "informational, not financial or regulated switching advice" framing so the client relays it.

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

## Tests

Three offline suites (standard-library `unittest`, no network):

| File | What it tests |
|------|---------------|
| `test_core.py` | The recommendation engine against a hand-built `Market`: archetype classification, ranking, Agile demotion, battery shifting, export handling, profile validation, UK time/BST handling, rate parsing, and a check that `core.py` stays free of CLI code. |
| `test_api.py` | The API layer against **saved real API responses**, with `core._get_json` replaced by a playback stub: product discovery and filtering, per-region tariff lookup, the `varying` payment-method key on Flexible Octopus, rate windows, pagination, retry and error handling, and end-to-end recommendations on recorded rates. |

| `test_mcp_server.py` | The MCP adapter through an in-memory MCP client, with the recorded API responses behind it: the tool list and schemas, results matching the core exactly, error mapping, validate-before-fetch, the rate cache (including concurrent callers), `find_region`, a real stdio subprocess handshake, and checks that the core never imports MCP and the adapter never prints or holds tariff logic. Skipped automatically when `mcp` is not installed. |

```bash
python -m unittest -v test_core test_api            # standard library only
.venv/bin/python -m unittest -v test_core test_api test_mcp_server   # everything, with mcp installed
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
- **Time-of-use import**: Agile Octopus (half-hourly), Octopus Go, Cosy Octopus
- **Export**: Outgoing Octopus and Agile Outgoing Octopus

Fixed-term and Intelligent Octopus Go products are not compared yet.

## How the recommendation works

The profile is classified into one of eight archetypes (no solar/EV/battery; solar only; EV overnight; EV daytime or flexible; solar + EV; solar + battery; solar + battery + EV; battery only). For each import tariff the engine simulates an average day: it places EV charging, generates solar, runs the battery, and prices what is left to buy from the grid. Import tariffs are ranked by estimated annual cost and export tariffs by estimated income, and each result lists the rate that applies, why it suits the profile, and caveats. Agile is not ranked first unless the profile can shift load (flexible EV charging or a battery that charges off-peak), because its price spikes cannot otherwise be avoided.

The assumptions used are returned with every result under `assumptions`.

## Result shape

`recommend_tariff()` returns a dict with `archetype`, `summary`, `recommendations` (top import and, for solar households, top export), `alternatives`, `notes`, `warnings`, `assumptions`, `data_source` and `disclaimer`. Each recommendation carries `rates`, `reasons` and `caveats` as data, so any interface can present them its own way.
