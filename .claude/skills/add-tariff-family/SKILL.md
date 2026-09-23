---
name: add-tariff-family
description: Add a new Octopus tariff family (product type) to the tariff-advisor's recommendation engine — use whenever Octopus Energy launches a new tariff (e.g. a new import or export product alongside Flexible, Agile, Go, Cosy, Outgoing, Agile Outgoing) and it needs to be discovered, priced, explained and ranked by tariff-advisor/core.py. Triggers on requests like "add support for Octopus's new tariff", "Octopus just launched X, can the advisor compare it", "add a new entry to FAMILIES", or "the advisor is missing tariff Y". Ensures every place in core.py that branches on a tariff's family string is updated together, not just the one or two that are obvious, and that the tests and docs that hardcode the family list don't go stale.
---

# Adding a new tariff family to the Octopus Tariff Advisor

## Why this skill exists

`core.py` has no polymorphism or per-family class hierarchy for tariffs —
deliberately, since six families didn't justify one. Instead, family-specific
behaviour is expressed as plain `tariff.family == "..."` or
`tariff.family in (...)` checks, scattered across several functions. That's
fine as long as every family-aware function is updated when a new family is
added. The risk is that only the *obvious* spot gets touched (usually
`FAMILIES` and one branch in `_build_tariff`), while a sibling check a few
hundred lines away — checking the exact same condition — is missed, and the
new tariff silently gets priced or explained as if it behaved like something
it doesn't. This skill is the checklist that catches that.

## Step 0: classify the new tariff first

Before touching any code, work out which existing family the new one
actually behaves like, from Octopus's docs or `docs.octopus.energy`:

| Behaviour | Existing example | What that implies |
|---|---|---|
| Rates change every half hour, unpredictably (wholesale-linked) | Agile, Agile Outgoing | It's "Agile-like" — see the coupled checks below |
| Fixed time-of-use windows (e.g. off-peak overnight, or a few price levels) | Go, Cosy | Behaves like the default case in most functions; only needs a caveat if it has a materially different peak/off-peak shape |
| Flat single rate | Flexible, Outgoing | Needs the least special-casing |
| Genuinely new shape (e.g. tiered by usage, a new EV-only window, dynamic on network signals) | — | Needs its own new branch, not just joining an existing tuple |

This classification decides which of the checklist items below actually
need a code change versus already working via the default path.

## The checklist

### 1. `FAMILIES` (core.py)

Add the entry: `{"direction": "IMPORT" | "EXPORT", "prefix": "...", "label": "..."}`.
The `prefix` must uniquely match the new product's code prefix and not
collide with any other family's prefix, including that family's own fixed-term
variants (e.g. `"GO-VAR"` deliberately doesn't match `GO-FIX-12M-*`). Check
against a live `/products/` listing, not assumption.

### 2. The Agile-like coupling — four sites, not one

If (and only if) the new family is Agile-like, it must be added to **all
four** of these, which independently test the same condition and will drift
apart if only some are updated:

- `_build_tariff`: the `if family in ("agile", "agile_outgoing"):` branch,
  which decides whether to fetch a trailing 7-day window (Agile-like) or the
  next 24 hours of current rates (everything else).
- `_build_tariff`: `is_agile = family in ("agile", "agile_outgoing")`, which
  decides whether the observed half-hourly min/max range is recorded on the
  `Tariff`.
- `_rates_summary`: `"cheapest_window_local": None if tariff.family in
  ("agile", "agile_outgoing") else ...` — an Agile-like tariff has no
  meaningful fixed cheap window, so this must stay `None` for it too.
- `_rates_summary`: the second `if tariff.family in ("agile",
  "agile_outgoing"):` just below it, which adds
  `observed_half_hour_range_p_kwh` to the summary.

If the new family is *not* Agile-like, none of these four need touching —
the default branches already handle fixed time-of-use and flat tariffs
correctly.

### 3. `_import_reasons` — three independent branches

This function builds the human-readable reasons and caveats, and has three
separate per-family special cases, not one:

- `if tariff.family == "agile":` — the half-hourly price description
  ("averaging Xp/kWh, last 7 days ranged..."). An Agile-like new family
  probably wants the same treatment; copy the pattern rather than reusing
  the literal string check if the new family should get its own wording.
- `if tariff.family in ("go", "cosy") and rs["max_p_kwh"] > ...:` — a
  caveat comparing this tariff's peak rate against the flat baseline. Decide
  whether the new family has a comparable "peak rate that can exceed the
  flat rate" characteristic and should join this tuple.
- `if tariff.family == "agile":` at the end — the volatility caveat
  ("prices change every half hour and can spike..."). Same question as
  above for an Agile-like family.

If the new family needs a caveat or reason none of the existing ones cover
(a new tariff's genuinely distinct selling point), add a new explicit
branch rather than forcing it into an existing one — don't let `agile`
become a catch-all for "anything volatile."

### 4. `build_recommendations` — two more family-specific rules

Easy to miss because they're in the ranking function, not the reasoning
function:

- The Agile demotion rule: `if ranked[0].family == "agile" and not
  _can_shift_load(profile)...` — Agile is deliberately not ranked first on
  average price alone unless the household can actually dodge its peaks.
  Ask whether the new family has the same "looks cheapest on average but
  has a real downside" property and deserves the same treatment.
- `caveats = [...] if tariff.family == "agile_outgoing" else []` — the
  "exports are not time-shifted" caveat. Only relevant for export tariffs
  with the same timing quirk.

### 5. Tests

- `test_api.py`'s `test_finds_all_six_families` hardcodes the exact set of
  families as an equality assertion — it will keep passing without ever
  exercising the new family unless you both add it to the expected set
  *and* update the "six" language. An equality check that isn't updated
  doesn't fail; it just silently stops being a real test.
- `test_api.py`'s `test_every_recorded_response_is_used` needs the fixture
  (`fixtures/api_responses.json`) to include the new family's product and
  rate endpoints. Add the `FAMILIES` entry first, then re-record with
  `python api_fixtures.py` (needs network) so discovery picks it up
  automatically, and update any assertions in `DiscoveryAndParsingTests`
  that check exact family sets or product codes.
- If you added a new branch in `_import_reasons` or `build_recommendations`
  (checklist items 3–4), add a synthetic tariff for it to `make_market()`
  in `test_core.py` and a targeted test — follow the pattern used for the
  `mixed` EV pattern's own test class (`MixedEvPatternTests`) as a template
  for "new behaviour gets its own test class, not just an extra assertion
  bolted onto an existing one."

### 6. Docs that hardcode the family list in prose

Both of these describe the compared tariffs by name and will read as wrong
(not just incomplete) if left stale:

- `mcp_server.py`, the `recommend_tariff` docstring — this is also what an
  MCP client sees as the tool description, so it directly affects what the
  agent tells a user is being compared.
- `README.md`, the "What it compares" section.

## Worked example (hypothetical)

Say Octopus launches "Octopus Tracker", a new IMPORT tariff whose rate
changes daily (not half-hourly) to track wholesale prices, with product
codes like `TRACKER-25-01-01`.

1. **Classify it**: changes daily, not half-hourly or fixed-window — closer
   to Agile in spirit (external-price-linked, not household-controllable)
   but not truly Agile-like at the half-hourly level, so it doesn't belong
   in the `("agile", "agile_outgoing")` tuples as-is. This is the "genuinely
   new shape" row from the Step 0 table.
2. **`FAMILIES`**: add
   `"tracker": {"direction": "IMPORT", "prefix": "TRACKER-", "label": "Octopus Tracker"}`.
3. **`_build_tariff`**: since it's daily, not half-hourly, and not
   Agile-like, decide what "current rate" means for it — probably today's
   single rate is genuinely flat for the day, similar to how Flexible is
   fetched, so it likely needs no new branch there (falls into the default
   `else`), but confirm the API actually returns one rate per day here
   rather than assuming.
4. **`_import_reasons`**: it isn't Agile, isn't Go/Cosy — its own reason
   text ("today's Tracker rate is Xp/kWh, changes once a day") is probably
   worth a new explicit branch rather than falling through to the generic
   flat-rate message, since "changes daily" is a caveat worth surfacing.
5. **`build_recommendations`**: does it deserve a demotion rule like Agile
   (cheap on average, but with a real downside a naive ranking would hide)?
   Judge on the tariff's actual behaviour, not by analogy alone.
6. **Tests and docs**: work through checklist items 5 and 6.

## Verification before shipping

- `python -m unittest test_core test_api` (and `test_mcp_server` in the
  venv, if `mcp` is installed) — all must pass, including the updated
  "six/seven families" assertions.
- Cross-check the new family's live rates against the raw API for at least
  one region, the way every prior tariff addition in this project has been
  verified: fetch the product detail and rate endpoints directly and
  compare standing charge and unit rate against what `core.py` reports.
- Consider a short mutation check on the branches you touched (temporarily
  break one of the four Agile-coupling sites, or the new caveat condition,
  in a scratch copy of `core.py`, and confirm the test suite actually
  fails) — this project's history shows that's the fastest way to find a
  checklist item you missed.
