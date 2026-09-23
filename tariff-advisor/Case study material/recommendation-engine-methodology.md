# How the tariff recommendation engine works

## Scope

This document explains the modelling inside `core.py` — the part that turns a
household profile and a set of live Octopus rates into a ranked recommendation.
It does not cover the CLI, the MCP server, or the test suite; those are
interfaces and safety nets around this engine, not the engine itself. All
figures, constants and function names below are taken directly from the
current code, not from memory of the design discussion.

The engine's job is narrower than it might sound: not "predict this
household's bill to the pound", but **rank a handful of published tariffs
against each other for one household, and say why**. Every modelling choice
below was made in service of that narrower job — enough realism to get the
ranking right, not so much that the tool pretends to know things it can't
(this household's roof orientation, weather, or exact appliance schedule).

## Pipeline, in one pass

1. `validate_profile` turns the raw input into a `Profile` (region, usage,
   solar, EV, battery — defaulted and flagged as assumed where allowed).
2. `fetch_market` pulls current rates for six tariff families and normalises
   each into 48 half-hourly prices (`Tariff.slot_rates_p_kwh`).
3. For **every** import tariff, `_cost_for_tariff` independently simulates one
   representative day of energy flows against *that tariff's own prices*, via
   `_dispatch_day`, and prices the result.
4. `build_recommendations` ranks the tariffs by estimated annual cost, ranks
   export tariffs separately by income, and builds the reasons/caveats
   directly from the same numbers used for costing.

The one design principle worth stating up front, because it shapes everything
downstream: **dispatch is simulated once per candidate tariff, not once for
the household and then re-priced.** A battery or a smart EV charger doesn't
behave the same way on a flat tariff as it does on Agile — it would sit idle
on a flat rate and hunt for cheap slots on Agile. Re-pricing one fixed energy
profile across tariffs would silently assume no one adapts their behaviour,
which understates exactly the tariffs (Go, Cosy, Agile) this tool exists to
evaluate. So every tariff gets its own simulation, and the household is
modelled as behaving rationally *for that tariff*.

## Time granularity: 48 half-hour slots, one representative day

Half-hourly is not a modelling choice so much as a constraint handed to us:
it's the UK electricity settlement period, and it's the exact granularity
Octopus publishes Agile, Go and Cosy rates at. Anything coarser would blur
the price boundaries that actually decide the recommendation — Go's off-peak
window ends at 23:30, and an hourly model can't represent that. Anything
finer would invent precision Octopus's own data doesn't have.

The simulation computes **one typical day** and multiplies by 365, rather
than simulating 365 separate days. For a time-of-use tariff and a repeating
demand/solar/EV shape, those are mathematically the same thing — the actual
approximation is not "one day vs many", it's that **demand and solar are
treated as non-seasonal**: the same shape every day of the year, scaled by
the household's single annual kWh figure. Winter's higher heating demand and
lower solar yield aren't represented. This is called out explicitly in
`ASSUMPTIONS["demand_shape"]` and in the limitations below — it's a
real simplification, not an oversight.

**Agile is the exception.** Instead of a stylised shape, its 48 slot rates
are the average of the actual last 7 days of published prices
(`agile_rates_basis`). Agile's whole character is volatility, so pretending
it fits a shape would misrepresent the one thing that matters about it. The
trade-off is that this is a recent snapshot, not a forecast — stated
directly in the tariff's own caveat ("this estimate uses a 7-day average").

## The demand shape

`_HOURLY_DEMAND_WEIGHTS` is a fixed, illustrative UK domestic load curve —
low overnight, a small morning bump, a pronounced evening peak around
18:00–20:00. It is explicitly labelled `"illustrative (not measured)"` in
`ASSUMPTIONS`, because it is: the tool has no access to this household's
actual consumption pattern, and asking for one would mean either collecting
smart-meter data (a much bigger, more invasive product — OAuth into the
customer's Octopus account, meter history, ongoing data handling) or asking
the user to describe their day hour by hour, which defeats the point of a
five-question tool.

One granularity detail worth flagging: the 24 hourly weights are each split
across their two half-hour slots **evenly**, not independently. The
simulation runs at half-hour resolution because the *prices* need that
resolution, but the underlying demand curve only has hourly resolution. Where
a tariff's price boundary falls mid-hour (Go's 23:30 cutoff, for instance),
the model can't say whether more of that hour's usage happened before or
after — it splits it 50/50. This slightly understates precision right at
tariff boundaries, though it doesn't systematically favour one tariff over
another, since the same curve is used for every candidate.

## Solar: why a sine curve instead of real irradiance data

This is worth answering directly, because "sine curve" sounds like a
shortcut next to "real weather data" — but the two solve different problems,
and only one of them is actually needed here.

The solar model has two separate parts:

- **Annual yield** (`solar_kwh_per_kwp_per_year = 850`): a standard UK
  rule-of-thumb figure for a reasonably sited array, in the same ballpark
  industry calculators and installers quote. This is where the real-world
  grounding lives.
- **Intraday shape** — how that annual total is spread across the 48 slots of
  the representative day: `_solar_shape` takes the daylight window
  (`solar_daylight_hours`, 06:00–20:00) and fits a half sine wave across it,
  peaking at solar noon and zero at both ends.

Real irradiance data (PVGIS-style hour-by-hour weather-adjusted output) would
improve the second part, but three things make it not worth the cost here:

1. **The inputs don't support it.** The household profile asks for array
   size in kWp — not roof orientation, tilt, or shading. Feeding a real
   irradiance dataset through a model that doesn't know which way the roof
   faces would produce a number that *looks* more precise without actually
   being more accurate for this household. That's worse than an honest
   approximation, not better.
2. **It's a different problem from the one being solved.** The tool's
   question is "does a time-of-use import tariff, or an export tariff, suit
   this profile" — which depends on *when* solar output clusters (all in
   daylight, none at night, peaking around midday), not on the exact shape of
   a particular week's cloud cover. Day-to-day weather variability mostly
   averages out over an annual estimate; the sine curve already gets the one
   thing that matters — no output overnight, most output near midday — right.
3. **It would add a dependency and a failure mode for a ranking that likely
   wouldn't change.** Octopus's API has no solar or weather data; sourcing
   it would mean a second external API, more latency, and another way the
   tool can fail, in exchange for an accuracy gain that mostly wouldn't move
   which tariff comes out on top.

A half sine wave, specifically, was chosen because it's the simplest curve
with the right qualitative properties — zero at sunrise and sunset, one
smooth peak in between — and it needs no calibration beyond the window
width. There's no seasonal adjustment to that window either (a fixed 14-hour
day all year, rather than ~8 hours in December and ~17 in June); that's
listed under limitations below.

## EV charging: four placement strategies

`_place_ev` spreads the household's daily EV kWh across the 48 slots
differently depending on `ev_charging_pattern`, each intended as a proxy for
a real charging behaviour rather than an arbitrary choice:

| Pattern | Window | Placement | Represents |
|---|---|---|---|
| `overnight` | 23:00–07:00 | Cheapest slots in the window filled first, up to the charger's per-slot limit, until the day's kWh is met | A scheduled/smart charger app targeting the cheapest hours within a fixed off-peak window (how Go/Cosy scheduled charging is actually used) |
| `flexible` | all 48 slots | Same cheapest-first fill, no window restriction | A charger or driver that can shift anytime to chase the best half-hour, suited to Agile or Cosy |
| `daytime` | 09:00–17:00 | Spread evenly across the window | Unscheduled daytime charging (e.g. while working from home); daytime rates are usually fairly flat, so rate-seeking wouldn't change much |
| `mixed` | both | 70% of the day's kWh via the `overnight` algorithm, the remaining 30% spread evenly across 17:00–22:00 | A driver who charges off-peak most nights but also tops up on arriving home, before any scheduling kicks in |

The per-slot fill limit comes from the assumed charger power
(`ev_charger_kw = 7.0`), which caps any half-hour slot at 3.5 kWh
(`7.0 kW × 0.5 h`) — a slot can't take more energy than the charger can
physically deliver in that time. `mixed` is implemented as a literal call
into the `overnight` branch for its off-peak share, plus a flat top-up for
the rest, rather than a separate algorithm — the same reasoning that applies
to `overnight` (cheapest-first, capacity-capped) applies unchanged to the
off-peak share of a mixed pattern.

The 70/30 split and the 17:00–22:00 "just got home" window are estimates,
not measured behaviour, and are listed in `ASSUMPTIONS` as such
(`ev_mixed_offpeak_share`, `ev_mixed_peak_window`).

## Battery logic: two distinct behaviours

The battery does two different jobs, and it's worth keeping them separate
because only one of them is "arbitrage" in the strict sense.

### 1. Solar self-consumption timing (always active if there's a battery)

Inside the main slot loop of `_dispatch_day`: any solar surplus left after
directly meeting that slot's demand charges the battery, up to its power
limit (`battery_max_kw = 3.0 kW` → 1.5 kWh per half-hour slot) and remaining
headroom. When there's unmet demand in a slot, the battery discharges to
cover it — but **only** if that slot's price is at or above the day's mean
rate for this tariff, or if the tariff is flat (in which case there's no
timing benefit to withholding, so it always helps to use your own stored
energy first). This is not arbitrage — it's just deciding *when* to spend
stored solar, so it's spent on expensive grid electricity rather than cheap.

This loop runs twice over the same day (`for day_pass in range(2)`), carrying
the battery's charge level from the end of the first pass into the second,
so the simulation isn't artificially forced to start every day with an empty
battery — a rough way to approximate a steady state without simulating
multiple actual days.

### 2. Grid arbitrage (only if `battery_can_shift_to_offpeak` is true)

This is a separate pass, run after self-consumption, and it's what the
household profile calls "the battery can charge from the grid at off-peak
times and discharge at peak":

1. Sort all 48 slots by price, cheapest first (`cheap_order`) and priciest
   first (`dear_order`).
2. Walk the cheap slots in order; for each one, walk the priciest slots in
   order and try to pair them: charge some energy in the cheap slot, use it
   to offset grid import in the pricy slot.
3. A pair is only made if it's actually profitable after round-trip losses:
   `import_rates[hi] * eff > import_rates[lo]` — you get back `eff` (0.9) of
   what you put in, so the expensive slot's rate has to beat the cheap
   slot's rate by more than the 10% loss for the trade to be worth making.
4. Each match is capped by three things at once: how much this cheap slot
   can still take this pass (`battery_max_kw` per slot), how much spare
   battery capacity is left overall, and how much the pricy slot actually
   needs (no point charging more than there's grid import left to displace).
5. Keep pairing until no profitable pair remains or capacity runs out.

**Why greedy pairing instead of a proper optimiser:** a linear program would
find a marginally better schedule, but it would mean adding an optimisation
library to a project that is deliberately dependency-free (the CLI and core
are standard-library only). Greedy cheapest-vs-priciest pairing is easy to
audit line by line, fast, deterministic, and for a single day of 48 points
it lands very close to the true optimum — the loss from not using an exact
solver is smaller than the loss from all the other simplifications in this
document.

**A deliberate simplification worth flagging:** arbitrage only uses capacity
*not already used* for solar self-consumption that day
(`free = battery_kwh - via_battery`). A real battery can charge and
discharge more than once in a day; this model treats the day's capacity as a
single pool shared between "storing solar" and "arbitrage", not something
that can be cycled twice. It's simpler to reason about and avoids overstating
what a modest domestic battery can actually do in one day, at the cost of
underestimating batteries that genuinely could do both.

## Ranking and explanation

Each import tariff's annual cost is `import_cost_gbp + standing_charge_gbp`
from its own `_cost_for_tariff` run; tariffs are sorted ascending by that
total. One adjustment is applied after sorting: if Agile comes out cheapest
on average but nothing in the profile lets the household actually dodge its
evening price spikes (no flexible EV charging, no battery that can shift),
it's moved to second place with an explicit note. An average-price ranking
alone would otherwise systematically favour Agile for households who
couldn't realistically avoid its worst half-hours — that would be a
misleading recommendation, not just an imprecise one.

Export tariffs are ranked separately, by estimated income against the
*dispatch already computed for the top-ranked import tariff* (there's only
one export profile to value, since export is physical solar surplus, not
tariff-dependent behaviour). If less than 100 kWh/year is left to export
after self-consumption and battery charging (`MIN_EXPORT_KWH`), no export
tariff is ranked at all — ranking a tariff on a near-zero income stream
would imply a precision the estimate doesn't have.

Every reason and caveat in the output is built from the same numbers used
for costing (`_import_reasons`), not generated separately — a tariff's
stated saving, EV average rate, or battery-shifted kWh is read directly out
of its `Breakdown`, so the explanation can't drift from the number it's
describing.

## Known limitations

- **No seasonality.** Demand and solar both use one fixed shape for the
  whole year; winter heating and shorter days aren't modelled.
- **No location-specific weather or roof geometry.** Solar yield uses a
  national rule-of-thumb annual figure and a stylised intraday shape; two
  households with the same kWp get the same estimate regardless of
  orientation, tilt or shading.
- **Demand curve is a national illustrative shape**, not this household's
  actual usage pattern, and only has hourly resolution mapped onto
  half-hourly prices.
- **Battery capacity is a single daily pool** shared between solar
  self-consumption and grid arbitrage, not something that can cycle twice.
- **Agile Outgoing export is valued at flat dispatch timing** — the model
  doesn't assume the household can shift *exports* to expensive half-hours,
  so Agile Outgoing's real value for an engaged household may be understated
  (`ASSUMPTIONS["export_timing"]`).
- **Agile's rate is a trailing 7-day average**, not a forecast, so it can be
  a poor guide during unusually cheap or expensive weeks.
- **Greedy, not optimal, arbitrage.** Close to optimal for one day of 48
  slots, but not provably the best possible schedule.

## Where the assumptions live

Every constant used above is a named entry in the single `ASSUMPTIONS`
dict at the top of `core.py`, and the whole dict is echoed back verbatim in
every result under `assumptions` — nothing that affects the numbers is
hidden inside the algorithm. The ones referenced in this document:

| Constant | Value | Governs |
|---|---|---|
| `days_per_year` | 365 | Annualising the one-day simulation |
| `demand_shape` | illustrative, not measured | Honesty label on the demand curve |
| `solar_kwh_per_kwp_per_year` | 850 | Annual solar yield per kWp |
| `solar_daylight_hours` | 06:00–20:00 | Width of the sine-curve window |
| `ev_charger_kw` | 7.0 | Per-slot EV charging cap (3.5 kWh/slot) |
| `ev_overnight_window` | 23:00–07:00 | `overnight` and off-peak share of `mixed` |
| `ev_daytime_window` | 09:00–17:00 | `daytime` pattern |
| `ev_mixed_offpeak_share` | 0.7 | Off-peak/peak split for `mixed` |
| `ev_mixed_peak_window` | 17:00–22:00 | Peak share of `mixed` |
| `battery_max_kw` | 3.0 | Per-slot charge/discharge cap (1.5 kWh/slot) |
| `battery_round_trip_efficiency` | 0.9 | Arbitrage profitability threshold |
| `agile_rates_basis` | last 7 days, averaged per slot | Agile's rate source |
| `export_timing` | exports not time-shifted | Agile Outgoing valuation caveat |

Changing any of these changes the numbers without touching the algorithm —
that separation is deliberate, so the modelling logic and the specific
figures it currently assumes can be reviewed and adjusted independently.
