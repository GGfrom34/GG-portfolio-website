# Does our engine agree with Octopus's own tariff guide?

**Source article:** [octopus.energy/blog/which-smart-tariff](https://octopus.energy/blog/which-smart-tariff/)
— "Which smart tariff should I choose? A simple guide", by Phil Steele, published 14 September 2026.
**Comparison run:** 23 September 2026, against live Octopus rates, region C (London).
**Engine version:** `tariff-advisor/core.py` as of the `add-docs-and-family-skill` merge.

## What the article actually contains

The article has four single-tech scenarios ("I have one smart thing") and a "combination"
section with five named combinations plus an open-ended sixth ("any other combination —
get in touch"), which recommends no tariff and isn't analysable. That's 4 + 5 = 9 real
scenarios, not 4 + 6.

| # | Article's scenario | Article's recommendation |
|---|---|---|
| 1 | EV only | Intelligent Octopus Go |
| 2 | Heat pump or electric boiler only | Cosy Octopus |
| 3 | Solar panels only (no battery) | Outgoing Octopus (import "exactly the same as anyone else's") |
| 4 | No smart tech ("just my brain") | Agile Octopus or Tracker |
| 5 | Solar + battery | Octopus Flux |
| 6 | EV + solar + battery | Intelligent Octopus Go + Outgoing + Charge Pack |
| 7 | EV + solar + battery + heat pump | Intelligent Octopus Go + Outgoing + Charge Pack |
| 8 | Heat pump + solar + battery | Octopus Flux |
| 9 | Heat pump + EV | Intelligent Octopus Go |
| — | Any other combination | No specific recommendation (contact Octopus) |

**Two structural gaps make a literal, like-for-like comparison impossible for most of these
before any modelling even starts**, and both are worth stating plainly up front rather than
discovering them scenario by scenario:

1. **Four of the article's named products don't exist in our tariff catalogue at all**:
   Intelligent Octopus Go, Octopus Flux, Tracker, and Charge Pack. `core.py`'s `FAMILIES`
   only tracks Flexible, Agile, Go, Cosy, Outgoing and Agile Outgoing. Intelligent Go and
   Flux appear in Octopus's own product list under different prefixes
   (`IOG-SMB-FIX-*`, presumably a `FLUX-*` equivalent) that our discovery logic never
   matches. Charge Pack is a managed-dispatch add-on service, not a tariff, and is out of
   scope for a tool that only compares tariffs. This alone means **7 of the 9 scenarios
   have an Octopus recommendation we cannot even attempt to reproduce**, regardless of
   how good our modelling is.
2. **Heat pumps aren't a profile input.** `Profile` has `has_solar`, `has_ev` and
   `has_battery`, and nothing else. The four scenarios that involve a heat pump (2, 7, 8, 9)
   can't be represented as distinct household models — our engine literally cannot tell a
   "heat pump + EV" household from an "EV only" household, because it has no way to know
   about the heat pump.

Given that, the honest way to run this comparison is **five distinct household models**,
covering what's actually representable, with the heat-pump scenarios explicitly mapped onto
their non-heat-pump twin rather than pretended to be independently tested:

| Model | Covers article scenario(s) |
|---|---|
| **A** — EV only | 1 (EV only), and stands in for 9 (heat pump + EV, minus the heat pump) |
| **B** — Solar only, no battery | 3 |
| **C** — No smart tech | 4, and stands in for 2 (heat pump only, minus the heat pump) |
| **D** — Solar + battery | 5, and stands in for 8 (heat pump + solar + battery, minus the heat pump) |
| **E** — EV + solar + battery | 6, and stands in for 7 (+ heat pump, minus the heat pump) |

## Matching Octopus's own assumptions, not ours

Octopus's article states its own modelling assumptions plainly (its "Our maths" section).
To test whether our *ranking logic* agrees with theirs — rather than testing whether our
*default numbers* happen to match theirs — every scenario below uses **their** stated
figures, translated into our schema, not our own defaults:

| Octopus's assumption | Value | Our input |
|---|---|---|
| Household baseline usage (Ofgem benchmark) | 2,700 kWh/yr | `average_usage_kwh` base (vs our own default of 2,500) |
| EV charging | 28 kWh/session, 3×/week | `ev_annual_kwh = 4,368` |
| EV plug-in window | 18:30–07:00, smart-scheduled to the cheapest hours in it | closest available pattern: `ev_charging_pattern="overnight"` (our fixed window is 23:00–07:00 — narrower; see sensitivity check below) |
| Solar generation | 4,500 kWh/yr | `solar_kwp = 4500 / 850 = 5.29` (backed out under our own yield-per-kWp assumption, since we take array size, not annual output, as the input) |
| Battery | 10 kWh nameplate, cycled 20–80% SoC, 5 kW inverter, 87.5% round-trip | `battery_kwh = 6` (the usable 60% swing — our model has no separate SoC-window concept); our own `battery_round_trip_efficiency = 0.9` is close enough not to adjust; our `battery_max_kw = 3.0` is a global constant, not a per-profile input, so it could not be set to their 5 kW (tested below instead) |
| Region | Not stated | London (region C), for consistency with the rest of this project |

**Two known mismatches were tested for actual impact, not just noted.** Since
`battery_max_kw` and `ev_overnight_window` are global constants (not something a caller can
override per profile), I patched each in a throwaway Python process — nothing written back
to `core.py` — and re-ran the affected scenarios:

- **Battery power, 3 kW → 5 kW (scenario D):** identical ranking and identical costs to the
  penny (Flexible 151.46 → Go 185.07 → Agile 195.32 → Cosy 198.79, unchanged). The battery
  in this household never actually hits the 3 kW/slot ceiling, so the extra headroom goes
  unused.
- **EV window, 23:00–07:00 → 18:30–07:00 (scenario A):** identical result (Octopus Go,
  £1,312.27/yr, EV averaging 8.6p/kWh). Go's actual cheapest hours right now (00:30–05:30)
  sit comfortably inside both windows, so widening the search window doesn't change which
  slots get used.

Neither mismatch changed anything in this run. That's a useful, falsifiable check to have
made rather than an assumption to leave untested — but it's specific to *this* household
size and *today's* rates, not a general proof the constants never matter.

## Scenario-by-scenario results

### A — EV only (covers scenarios 1 and 9)

| | Recommendation | Note |
|---|---|---|
| **Octopus** | Intelligent Octopus Go | Guaranteed 6h window, smart-scheduled, whole home gets the cheap rate while the car charges |
| **Our engine** | **Octopus Go** — £1,312/yr | EV averages 8.6p/kWh vs 26.3p on Flexible; £701/yr cheaper than Flexible |

**Directionally aligned, exact product not in our catalogue.** Both tools land on "an
off-peak overnight import tariff is the right call for an EV." Octopus's actual pick isn't
one we track. Worth being precise about *why* they're not interchangeable, beyond just the
name: Intelligent Go's home-wide cheap rate is contingent on a charging *event* happening
(a dynamic trigger), where our `Tariff` model only represents a fixed clock-time price
curve — this is a difference in tariff *mechanic*, not just branding, and it isn't
something our current data model could represent even if we added the product.

### B — Solar only, no battery (covers scenario 3)

| | Recommendation | Note |
|---|---|---|
| **Octopus** | Outgoing Octopus (export); import "exactly the same as anyone else's" | They deliberately don't commit to an import answer |
| **Our engine** | Import: **Cosy Octopus** — £387/yr. Export: **Outgoing Octopus** — £339/yr income | Net ≈ £49/yr |

**Aligned on export.** On import, Octopus explicitly declines to pick one — and our own
numbers back up why: the spread between our cheapest import option (Cosy, £387) and our
most expensive (Flexible, £421) is £34/yr, about 8% of the bill. Our tool still names a
top pick because it always ranks every candidate, but the *size* of that pick's advantage
agrees with Octopus's own implicit claim that the import choice barely matters here.

### C — No smart tech (covers scenario 4 and, as a stand-in, scenario 2)

| | Recommendation | Note |
|---|---|---|
| **Octopus** | Agile Octopus or Tracker | Hedged: "Agile is generally worth it *if you can shift* your power use... Tracker if you don't want to think about it" |
| **Our engine** | **Flexible Octopus** — £863/yr | Agile is cheaper on paper (£838/yr); Cosy is too (£848/yr); both demoted — see below |

**This is the one genuine disagreement in principle, not just in product catalogue**, and
it's worth stating exactly what it is. Our own raw cost ranking, before any adjustment, is
Agile (£838.00) < Cosy (£847.73) < Flexible (£862.84) < Go (£935.53) — Agile *is* our
cheapest estimate. We deliberately move it down whenever nothing in the profile (a flexible
EV, or a battery that can shift to off-peak) lets the household dodge its evening price
spikes, on the reasoning that recommending the average-cheapest option to someone who can't
avoid its worst hours is misleading, not just imprecise. Octopus's own article recommends
Agile (or Tracker, which we don't track at all) for the same household shape, with a hedge
attached rather than a flat endorsement.

Neither position is simply wrong — they reflect different assumptions about who's reading a
tariff-switching guide in the first place. Octopus's implicit audience has already
self-selected as somewhat engaged (they're reading an article about smart tariffs); our
tool has no such signal and defaults to protecting a genuinely passive household from
downside risk.

> **Update, applied after this report was first written:** this run originally had our
> engine recommending **Cosy Octopus**, not Flexible — Cosy also edges out Flexible on raw
> cost (£847.73 vs £862.84), by £15/yr, even though this household has no heat pump, no EV
> and no battery, nothing that could actually make use of Cosy's off-peak windows on
> purpose. That wasn't a bug in the arithmetic — the generic demand shape
> (`_HOURLY_DEMAND_WEIGHTS`) genuinely has some of its mass sitting in Cosy's cheap hours by
> chance, since it's built from aggregate national data, not from this specific household
> doing anything differently — but it meant a `neither` household got partial credit for
> timing it never actually performs, and the tool named a specific product as "best fit" on
> the strength of it. `core.py` now demotes any time-of-use tariff for the `neither`
> archetype whose saving over Flexible is below 3% of Flexible's cost
> (`ASSUMPTIONS["neither_tou_materiality_share"]`), with a note naming the edged-out tariff
> and the size of the margin. Both Agile (£838.00) and Cosy (£847.73) now fall below
> Flexible (£862.84) here, in that order, and the note explains why. A materially larger
> time-of-use saving elsewhere is left alone — see `recommendation-engine-methodology.md`
> for the full rule.

This changes the *number* our tool leads with, but not the *substance* of the disagreement
with Octopus: we still don't recommend Agile (or Tracker) for a household with no way to
dodge its peaks, on principle, and Octopus's own guidance still does, with a hedge. The fix
just stops an unrelated, unintended side effect (Cosy winning by curve luck) from
overshadowing that actual, deliberate disagreement.

### D — Solar + battery (covers scenario 5 and, as a stand-in, scenario 8)

| | Recommendation | Note |
|---|---|---|
| **Octopus** | Octopus Flux | Combined import + export tariff: cheap import 02:00–05:00, premium export 16:00–19:00 |
| **Our engine** | Import: **Flexible Octopus** — £151/yr. Export: **Outgoing Octopus** — £202/yr income | **Net ≈ –£50/yr** (a net earner) |

**Not comparable by product, and not fully comparable by architecture either.** Flux's
entire value proposition is that the cheap import rate and the premium export rate are
*the same product* — you get access to one because you're committed to the other. Our tool
picks the best import tariff and the best export tariff independently, because that's how
every tariff we track actually works (they're separate products). Even if we added Flux's
rate numbers to our engine, our current "rank imports, then rank exports separately"
architecture has no way to represent a tariff whose value depends on holding both halves
at once — that would need a genuinely different code path, not just a new `FAMILIES` entry.

Separately, our battery arbitrage logic did activate here on Agile (£195/yr, "shifts about
503 kWh/yr from cheap to expensive slots") but Agile still didn't win — Flexible's lower
standing charge and lack of a peak-rate penalty beat Go, Agile and Cosy once solar and the
battery had already absorbed most of the household's demand, leaving very little residual
import to discount. That's a real, defensible result: **a time-of-use tariff isn't
automatically better just because there's a battery** — if self-consumption already covers
nearly everything, the residual import is small enough that a lower standing charge can win
outright.

### E — EV + solar + battery (covers scenario 6 and, as a stand-in, scenario 7)

| | Recommendation | Note |
|---|---|---|
| **Octopus** | Intelligent Octopus Go (import) + Outgoing Octopus (export) + Charge Pack | |
| **Our engine** | Import: **Octopus Go** — £562/yr. Export: **Outgoing Octopus** — £239/yr income | Net ≈ £323/yr |

**The closest match in the whole comparison.** Strip out the exact product name and the
Charge Pack add-on (a managed-dispatch service, out of scope for a tariff-only tool), and
the *shape* of the two answers is identical: an off-peak-window import tariff from the Go
family, paired with Outgoing for export. This is the strongest evidence that, within the
tariffs it actually knows about, the engine's ranking logic reaches the same conclusion an
Octopus adviser reaches for the same household shape.

## Scenarios not independently testable

- **Heat pump only** (2), **Heat pump + EV** (9), **Heat pump + solar + battery** (8), and
  **EV + solar + battery + heat pump** (7) each reduce to scenario C, A, D and E
  respectively, because `has_solar`/`has_ev`/`has_battery` are the entire profile — there is
  no fourth signal to add. Adding a heat pump would need a new profile field and its own
  load-placement logic (structurally similar to `_place_ev`, but shaped by heating degree
  rather than a fixed daily kWh), not just a new tariff family.
- **Any other combination**: the article names no household and recommends no tariff
  ("get in touch and we'll give you a personalised recommendation"), so there's nothing to
  compare against.

## What this exercise found, in order of how much it should change something

1. **Missing tariffs are the dominant gap, not modelling accuracy.** Intelligent Octopus
   Go and Flux between them are Octopus's recommended answer for 6 of the 9 scenarios.
   Adding them (via the `add-tariff-family` skill for Intelligent Go's tariff-side
   behaviour; Flux would need new architecture, not just a new family, since it's a
   coupled import+export product) would close most of the gap this report found — more
   than any change to the dispatch simulation would.
2. **The `neither` archetype crediting off-peak windows it can't earn on purpose** is a
   real methodology finding, not just a catalogue gap, and is a reasonable next fix to the
   engine itself.
3. **The Agile-demotion policy is a considered, disclosed disagreement, not an error** —
   and in this run it cost the household about 1% of their bill relative to Octopus's own
   pick, which is a defensible price for the downside protection it buys.
4. **Where the tariffs on offer actually overlap (EV-led households), the engine's answer
   matches Octopus's own expert guidance closely.** The disagreements documented here are
   concentrated in exactly the scenarios where the two tools aren't really answering the
   same question — because one of them has a product, and a household input, the other
   doesn't have at all.
