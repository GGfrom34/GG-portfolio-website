"""Octopus Tariff Advisor: core logic.

Informational tool only. It is not financial advice or regulated switching
advice, and it is not affiliated with Octopus Energy.

This module has no knowledge of any user interface: no argparse, no printing,
no prompts, no sys.exit. Any front end (the CLI, a future MCP server) calls
`recommend_tariff(profile)` and presents the returned plain-data dict itself.

Layout, with dependencies pointing one way (API layer -> data -> engine):

    1. Constants and assumptions
    2. Data types
    3. API layer            the only code that touches the network
    4. Recommendation engine pure functions over a Profile and a Market
    5. Public entry point   recommend_tariff()
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 1. Constants and assumptions
# ---------------------------------------------------------------------------

API_BASE = "https://api.octopus.energy/v1"
HTTP_TIMEOUT_S = 20
HTTP_RETRIES = 2
SLOTS = 48  # half-hour slots in a local (UK) day; slot 0 is 00:00-00:30
MIN_EXPORT_KWH = 100.0  # below this, ranking export tariffs is not meaningful

DISCLAIMER = (
    "Informational only. This is not financial advice or regulated switching "
    "advice, and it is not affiliated with Octopus Energy. Estimates rest on the "
    "assumptions listed in this result; check octopus.energy for current terms."
)

# GSP region letters -> names (used for validation and messages).
REGIONS = {
    "A": "Eastern England", "B": "East Midlands", "C": "London",
    "D": "Merseyside and North Wales", "E": "West Midlands",
    "F": "North East England", "G": "North West England",
    "H": "Southern England", "J": "South East England",
    "K": "South Wales", "L": "South West England", "M": "Yorkshire",
    "N": "Southern Scotland", "P": "Northern Scotland",
}

# Every modelling assumption lives here and is echoed back in the result.
ASSUMPTIONS: dict[str, Any] = {
    "usage_is_total_annual_kwh_including_ev": True,
    "ev_default_kwh_per_week": 40.0,
    "ev_charger_kw": 7.0,
    "ev_overnight_window": "23:00-07:00",
    "ev_daytime_window": "09:00-17:00",
    "ev_max_share_of_usage": 0.7,
    "solar_kwh_per_kwp_per_year": 850.0,
    "solar_default_kwp": 4.0,
    "solar_daylight_hours": "06:00-20:00",
    "export_capacity_kw_treated_as": "solar array size (kWp)",
    "battery_default_kwh": 5.0,
    "battery_max_kw": 3.0,
    "battery_round_trip_efficiency": 0.9,
    "battery_arbitrage_uses_capacity_not_used_for_solar": True,
    "agile_rates_basis": "average of the last 7 days per half-hour slot",
    "time_of_use_rates_basis": "rates currently in force, mapped to UK local time",
    "export_timing": "exports are not time-shifted, so Agile Outgoing is undervalued",
    "days_per_year": 365,
    "demand_shape": "typical UK domestic day (illustrative, not measured)",
}

# Illustrative UK domestic demand by hour of day (relative weights).
_HOURLY_DEMAND_WEIGHTS = [
    2.5, 2.2, 2.0, 2.0, 2.1, 2.6, 3.6, 4.8, 5.0, 4.6, 4.3, 4.3,
    4.4, 4.3, 4.3, 4.8, 6.0, 7.6, 8.0, 7.4, 6.2, 5.2, 4.0, 3.0,
]

# Product families we look for. Product codes are dated and rotate, so we
# discover the current one by prefix + direction rather than hardcoding a code.
FAMILIES: dict[str, dict[str, str]] = {
    "variable": {"direction": "IMPORT", "prefix": "VAR-", "label": "Flexible Octopus (standard variable)"},
    "agile": {"direction": "IMPORT", "prefix": "AGILE-", "label": "Agile Octopus"},
    "go": {"direction": "IMPORT", "prefix": "GO-VAR", "label": "Octopus Go"},
    "cosy": {"direction": "IMPORT", "prefix": "COSY-2", "label": "Cosy Octopus"},
    "outgoing": {"direction": "EXPORT", "prefix": "OUTGOING-VAR", "label": "Outgoing Octopus"},
    "agile_outgoing": {"direction": "EXPORT", "prefix": "AGILE-OUTGOING", "label": "Agile Outgoing Octopus"},
}

ARCHETYPES: dict[str, dict[str, Any]] = {
    "neither": {
        "title": "No solar, EV or battery",
        "explanation": "With no flexible load or generation, a flat variable rate is the baseline other tariffs are measured against; time-of-use tariffs only win if your usage happens to fall in their cheap windows or you can move it there.",
    },
    "solar_only": {
        "title": "Solar panels only",
        "explanation": "Solar output is mostly used or exported in the daytime, so cheap overnight import rates add little; the export rate matters more.",
    },
    "ev_overnight": {
        "title": "EV charged overnight",
        "explanation": "An EV is a large, schedulable load; charging overnight lets it use a cheap night rate.",
    },
    "ev_daytime_flexible": {
        "title": "EV charged in the daytime or flexibly",
        "explanation": "A fixed overnight window only helps if charging can move there. Daytime charging suits a flat rate, and flexible charging can follow half-hourly prices on Agile or Cosy.",
    },
    "solar_ev": {
        "title": "Solar panels plus an EV",
        "explanation": "Solar covers some daytime demand and the EV can use a cheap overnight window, so a time-of-use import tariff paired with an export tariff usually fits.",
    },
    "solar_battery": {
        "title": "Solar panels plus a battery",
        "explanation": "The battery stores surplus solar, cutting imports. If it can also charge from the grid at cheap times, time-of-use tariffs become viable for the remaining import.",
    },
    "solar_battery_ev": {
        "title": "Solar panels, battery and EV",
        "explanation": "The EV takes cheap overnight power, solar and the battery cover daytime and evening demand, and a shiftable battery can top up in cheap slots.",
    },
    "battery_only": {
        "title": "Battery without solar",
        "explanation": "A battery that can charge at cheap times and discharge at peak can profit from the spread between rates. Without that ability it gives no tariff advantage.",
    },
}


# ---------------------------------------------------------------------------
# 2. Data types
# ---------------------------------------------------------------------------

class TariffAdvisorError(Exception):
    """Base class for errors raised by this module."""


class ProfileError(TariffAdvisorError, ValueError):
    """The household profile is missing a field or has an invalid value."""


class OctopusApiError(TariffAdvisorError):
    """The Octopus API could not be reached or returned unusable data."""


@dataclass
class Profile:
    region: str
    average_usage_kwh: float
    has_solar: bool
    has_ev: bool
    export_capacity_kw: Optional[float] = None
    ev_charging_pattern: Optional[str] = None
    ev_kwh_per_week: Optional[float] = None
    has_battery: bool = False
    battery_kwh: Optional[float] = None
    battery_can_shift_to_offpeak: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class Tariff:
    """One tariff for one region, normalised from the API.

    `slot_rates_p_kwh` has 48 entries, one per local half-hour (00:00 first),
    pence per kWh including VAT.
    """
    family: str
    direction: str
    name: str
    product_code: str
    tariff_code: str
    standing_charge_p_day: float
    slot_rates_p_kwh: list[float]
    rate_basis: str
    source_urls: list[str]
    observed_min_p_kwh: Optional[float] = None  # Agile only: raw half-hour range
    observed_max_p_kwh: Optional[float] = None


@dataclass
class Market:
    region: str
    fetched_at: str
    import_tariffs: list[Tariff]
    export_tariffs: list[Tariff]
    unavailable: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 3. API layer (the only code that touches the network)
# ---------------------------------------------------------------------------

def _get_json(url: str, params: Optional[dict[str, Any]] = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_error: Optional[Exception] = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "octopus-tariff-advisor/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()  # release the connection now instead of at garbage collection
            if exc.code < 500:  # client errors will not fix themselves on retry
                raise OctopusApiError(f"Octopus API returned HTTP {exc.code} for {url}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
        if attempt < HTTP_RETRIES:
            time.sleep(1.5 * (attempt + 1))
    raise OctopusApiError(f"Could not reach the Octopus API ({url}): {last_error}") from last_error


def _paginate(url: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Collect `results` across pages by following the `next` link."""
    results: list[dict[str, Any]] = []
    page = _get_json(url, params)
    while True:
        results.extend(page.get("results", []))
        next_url = page.get("next")
        if not next_url:
            return results
        page = _get_json(next_url)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _uk_offset(utc: datetime) -> timedelta:
    """UK local offset: BST from 01:00 UTC on the last Sunday of March to the last Sunday of October."""
    def last_sunday(month: int) -> datetime:
        d = datetime(utc.year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return timedelta(hours=1) if last_sunday(3) <= utc < last_sunday(10) else timedelta(0)


def _local_slot(utc: datetime) -> int:
    local = utc + _uk_offset(utc)
    return local.hour * 2 + local.minute // 30


def _slot_rates_from_records(records: list[dict[str, Any]], start: datetime, end: datetime) -> tuple[list[float], list[float]]:
    """Expand rate records over [start, end) into per-local-slot averages.

    Returns (48 slot averages, every raw half-hour price seen). Records for
    non-direct-debit payment are ignored.
    """
    usable = []
    for r in records:
        if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
            continue
        valid_to = _parse_utc(r["valid_to"]) if r.get("valid_to") else None
        usable.append((_parse_utc(r["valid_from"]), valid_to, float(r["value_inc_vat"])))
    per_slot: list[list[float]] = [[] for _ in range(SLOTS)]
    raw: list[float] = []
    t = start
    while t < end:
        for valid_from, valid_to, price in usable:
            if valid_from <= t and (valid_to is None or t < valid_to):
                per_slot[_local_slot(t)].append(price)
                raw.append(price)
                break
        t += timedelta(minutes=30)
    if not raw:
        raise OctopusApiError("No unit rates returned for the requested window")
    overall = sum(raw) / len(raw)
    return [sum(p) / len(p) if p else overall for p in per_slot], raw


def _discover_products(now: datetime) -> dict[str, dict[str, Any]]:
    """Pick the newest currently-available product for each family."""
    products = _paginate(f"{API_BASE}/products/", {"brand": "OCTOPUS_ENERGY", "is_business": "false", "page_size": 100})
    chosen: dict[str, dict[str, Any]] = {}
    for family, spec in FAMILIES.items():
        candidates = [
            p for p in products
            if p.get("direction") == spec["direction"]
            and p.get("code", "").startswith(spec["prefix"])
            and not p.get("is_prepay")
            and _parse_utc(p["available_from"]) <= now
            and (not p.get("available_to") or _parse_utc(p["available_to"]) > now)
        ]
        if candidates:
            chosen[family] = max(candidates, key=lambda p: p["available_from"])
    return chosen


def _build_tariff(family: str, product: dict[str, Any], region: str, now: datetime) -> Optional[Tariff]:
    code = product["code"]
    detail = _get_json(f"{API_BASE}/products/{code}/")
    by_region = detail.get("single_register_electricity_tariffs", {}).get(f"_{region}")
    if not by_region:
        return None
    entry = by_region.get("direct_debit_monthly") or next(iter(by_region.values()))
    tariff_code = entry["code"]
    rates_url = f"{API_BASE}/products/{code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    if family in ("agile", "agile_outgoing"):
        end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        start = end - timedelta(days=7)
        basis = ASSUMPTIONS["agile_rates_basis"]
    else:
        start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        end = start + timedelta(hours=24)
        basis = ASSUMPTIONS["time_of_use_rates_basis"]
    records = _paginate(rates_url, {
        "period_from": start.strftime("%Y-%m-%dT%H:%MZ"),
        "period_to": end.strftime("%Y-%m-%dT%H:%MZ"),
        "page_size": 1500,
    })
    slot_rates, raw = _slot_rates_from_records(records, start, end)
    is_agile = family in ("agile", "agile_outgoing")
    return Tariff(
        family=family,
        direction=product["direction"],
        name=product.get("full_name") or FAMILIES[family]["label"],
        product_code=code,
        tariff_code=tariff_code,
        standing_charge_p_day=float(entry.get("standing_charge_inc_vat") or 0.0),
        slot_rates_p_kwh=slot_rates,
        rate_basis=basis,
        source_urls=[f"{API_BASE}/products/{code}/", rates_url],
        observed_min_p_kwh=min(raw) if is_agile else None,
        observed_max_p_kwh=max(raw) if is_agile else None,
    )


def fetch_market(region: str, *, now: Optional[datetime] = None) -> Market:
    """Pull current rates for standard variable, Agile, Go, Cosy and Outgoing tariffs."""
    now = now or datetime.now(timezone.utc)
    chosen = _discover_products(now)
    imports: list[Tariff] = []
    exports: list[Tariff] = []
    unavailable: list[str] = []
    for family in FAMILIES:
        product = chosen.get(family)
        tariff = _build_tariff(family, product, region, now) if product else None
        if tariff is None:
            unavailable.append(FAMILIES[family]["label"])
        elif tariff.direction == "EXPORT":
            exports.append(tariff)
        else:
            imports.append(tariff)
    if not imports:
        raise OctopusApiError(f"No import tariffs found for region _{region}")
    return Market(region=region, fetched_at=now.isoformat(), import_tariffs=imports, export_tariffs=exports, unavailable=unavailable)


# ---------------------------------------------------------------------------
# 4. Recommendation engine (pure functions: no network, no printing)
# ---------------------------------------------------------------------------

@dataclass
class Breakdown:
    """Annual energy flows and cost for one import tariff, in kWh and pence."""
    grid_import_kwh: float
    ev_kwh: float
    ev_avg_p_kwh: Optional[float]
    solar_generated_kwh: float
    solar_self_used_kwh: float
    solar_exported_kwh: float
    battery_from_solar_kwh: float
    battery_shifted_kwh: float
    import_cost_gbp: float
    standing_charge_gbp: float
    export_by_slot_kwh: list[float]


def _hhmm_to_slot(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 2 + int(m) // 30


def _window_slots(window: str) -> list[int]:
    """Slots in a 'HH:MM-HH:MM' window; a window that crosses midnight wraps."""
    start, end = (_hhmm_to_slot(x) for x in window.split("-"))
    if end <= start:
        end += SLOTS
    return [s % SLOTS for s in range(start, end)]


def _demand_shape() -> list[float]:
    slots = [w for w in _HOURLY_DEMAND_WEIGHTS for _ in range(2)]
    total = sum(slots)
    return [s / total for s in slots]


def _solar_shape() -> list[float]:
    slots = _window_slots(ASSUMPTIONS["solar_daylight_hours"])
    weights = [0.0] * SLOTS
    for i, s in enumerate(slots):
        weights[s] = math.sin(math.pi * (i + 0.5) / len(slots))
    total = sum(weights)
    return [w / total for w in weights]


def _is_flat(rates: list[float]) -> bool:
    return max(rates) - min(rates) < 1e-6


def _place_ev(pattern: str, daily_kwh: float, rates: list[float]) -> list[float]:
    """Spread the EV's daily energy over slots according to how it is charged."""
    load = [0.0] * SLOTS
    per_slot = ASSUMPTIONS["ev_charger_kw"] * 0.5
    if pattern == "daytime":
        slots = _window_slots(ASSUMPTIONS["ev_daytime_window"])
        for s in slots:
            load[s] = daily_kwh / len(slots)
        return load
    allowed = _window_slots(ASSUMPTIONS["ev_overnight_window"]) if pattern == "overnight" else list(range(SLOTS))
    remaining = daily_kwh
    for s in sorted(allowed, key=lambda i: (rates[i], i)):  # cheapest slots first
        take = min(per_slot, remaining)
        load[s] = take
        remaining -= take
        if remaining <= 1e-9:
            break
    return load


def _dispatch_day(demand: list[float], solar: list[float], import_rates: list[float],
                  battery_kwh: float, battery_shifts: bool) -> tuple[list[float], list[float], float, float, float]:
    """Simulate one average day.

    Returns (grid import per slot, export per slot, solar self-used kWh,
    solar kWh routed via battery, kWh shifted by grid arbitrage).
    """
    eff = ASSUMPTIONS["battery_round_trip_efficiency"]
    step_cap = ASSUMPTIONS["battery_max_kw"] * 0.5
    mean_rate = sum(import_rates) / SLOTS
    flat = _is_flat(import_rates)
    soc = 0.0
    grid = [0.0] * SLOTS
    export = [0.0] * SLOTS
    self_used = via_battery = 0.0
    for day_pass in range(2):  # second pass starts with the battery state carried from the first
        self_used = via_battery = 0.0
        for i in range(SLOTS):
            direct = min(solar[i], demand[i])
            surplus, need = solar[i] - direct, demand[i] - direct
            self_used += direct
            if battery_kwh > 0:
                charge = min(surplus, step_cap, battery_kwh - soc)
                soc += charge
                surplus -= charge
                via_battery += charge
                if need > 0 and (flat or import_rates[i] > mean_rate):
                    drawn = min(soc, step_cap, need / eff)
                    soc -= drawn
                    need -= drawn * eff
            grid[i], export[i] = need, surplus
    shifted = 0.0
    if battery_kwh > 0 and battery_shifts:
        free = max(0.0, battery_kwh - via_battery)
        charge_room = {i: step_cap for i in range(SLOTS)}
        cheap_order = sorted(range(SLOTS), key=lambda i: import_rates[i])
        dear_order = sorted(range(SLOTS), key=lambda i: -import_rates[i])
        for lo in cheap_order:
            for hi in dear_order:
                if free <= 1e-9 or charge_room[lo] <= 1e-9:
                    break
                if import_rates[hi] * eff <= import_rates[lo] or grid[hi] <= 1e-9:
                    continue
                x = min(charge_room[lo], free, grid[hi] / eff)
                grid[lo] += x
                grid[hi] -= x * eff
                charge_room[lo] -= x
                free -= x
                shifted += x
    return grid, export, self_used, via_battery, shifted


def _cost_for_tariff(profile: Profile, tariff: Tariff) -> Breakdown:
    days = ASSUMPTIONS["days_per_year"]
    usage = profile.average_usage_kwh
    ev_year = 0.0
    if profile.has_ev:
        ev_year = (profile.ev_kwh_per_week or ASSUMPTIONS["ev_default_kwh_per_week"]) * 52
        ev_year = min(ev_year, usage * ASSUMPTIONS["ev_max_share_of_usage"])
    base_daily = (usage - ev_year) / days
    rates = tariff.slot_rates_p_kwh
    demand = [base_daily * s for s in _demand_shape()]
    ev_load = [0.0] * SLOTS
    if profile.has_ev and ev_year > 0:
        ev_load = _place_ev(profile.ev_charging_pattern or "flexible", ev_year / days, rates)
        demand = [d + e for d, e in zip(demand, ev_load)]
    gen_year = 0.0
    solar = [0.0] * SLOTS
    if profile.has_solar:
        kwp = profile.export_capacity_kw or ASSUMPTIONS["solar_default_kwp"]
        gen_year = kwp * ASSUMPTIONS["solar_kwh_per_kwp_per_year"]
        solar = [gen_year / days * s for s in _solar_shape()]
    battery = (profile.battery_kwh or ASSUMPTIONS["battery_default_kwh"]) if profile.has_battery else 0.0
    grid, export, self_used, via_battery, shifted = _dispatch_day(demand, solar, rates, battery, profile.battery_can_shift_to_offpeak)
    ev_cost_p = sum(e * r for e, r in zip(ev_load, rates))
    ev_daily = sum(ev_load)
    return Breakdown(
        grid_import_kwh=sum(grid) * days,
        ev_kwh=ev_year,
        ev_avg_p_kwh=(ev_cost_p / ev_daily) if ev_daily > 0 else None,
        solar_generated_kwh=gen_year,
        solar_self_used_kwh=self_used * days,
        solar_exported_kwh=sum(export) * days,
        battery_from_solar_kwh=via_battery * days,
        battery_shifted_kwh=shifted * days,
        import_cost_gbp=sum(g * r for g, r in zip(grid, rates)) * days / 100,
        standing_charge_gbp=tariff.standing_charge_p_day * days / 100,
        export_by_slot_kwh=[e * days for e in export],
    )


def classify_profile(profile: Profile) -> str:
    """Map a profile to one of the archetype keys in ARCHETYPES."""
    if profile.has_solar and profile.has_battery and profile.has_ev:
        return "solar_battery_ev"
    if profile.has_solar and profile.has_battery:
        return "solar_battery"
    if profile.has_solar and profile.has_ev:
        return "solar_ev"
    if profile.has_solar:
        return "solar_only"
    if profile.has_ev:  # an EV outweighs a battery when there is no solar
        return "ev_overnight" if profile.ev_charging_pattern == "overnight" else "ev_daytime_flexible"
    if profile.has_battery and profile.battery_can_shift_to_offpeak:
        return "battery_only"
    return "neither"


def _can_shift_load(profile: Profile) -> bool:
    return (profile.has_battery and profile.battery_can_shift_to_offpeak) or (profile.has_ev and profile.ev_charging_pattern == "flexible")


def _cheapest_window(rates: list[float], tolerance: float = 1e-6) -> Optional[str]:
    """Longest contiguous run of slots at the minimum rate, as 'HH:MM-HH:MM' (None if flat)."""
    if _is_flat(rates):
        return None
    low = min(rates)
    at_low = [r <= low + tolerance for r in rates] * 2
    best_start = best_len = 0
    run_start = None
    for i, flag in enumerate(at_low):
        if flag and run_start is None:
            run_start = i
        if (not flag or i == len(at_low) - 1) and run_start is not None:
            length = min(i - run_start + (1 if flag else 0), SLOTS)
            if length > best_len:
                best_start, best_len = run_start, length
            run_start = None
    fmt = lambda s: f"{(s % SLOTS) // 2:02d}:{(s % SLOTS) % 2 * 30:02d}"
    return f"{fmt(best_start)}-{fmt(best_start + best_len)}"


def _rates_summary(tariff: Tariff) -> dict[str, Any]:
    rates = tariff.slot_rates_p_kwh
    summary: dict[str, Any] = {
        "standing_charge_p_day": round(tariff.standing_charge_p_day, 2),
        "min_p_kwh": round(min(rates), 2),
        "max_p_kwh": round(max(rates), 2),
        "average_p_kwh": round(sum(rates) / SLOTS, 2),
        # A 7-day average has no meaningful fixed window, so only scheduled tariffs report one.
        "cheapest_window_local": None if tariff.family in ("agile", "agile_outgoing") else _cheapest_window(rates),
        "basis": tariff.rate_basis,
    }
    if tariff.family in ("agile", "agile_outgoing"):
        lo = tariff.observed_min_p_kwh if tariff.observed_min_p_kwh is not None else min(rates)
        hi = tariff.observed_max_p_kwh if tariff.observed_max_p_kwh is not None else max(rates)
        summary["observed_half_hour_range_p_kwh"] = [round(lo, 2), round(hi, 2)]
    return summary


def _import_reasons(profile: Profile, tariff: Tariff, bd: Breakdown, baseline: Optional[tuple[Tariff, Breakdown]]) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    caveats: list[str] = []
    rs = _rates_summary(tariff)
    if tariff.family == "agile":
        lo, hi = rs["observed_half_hour_range_p_kwh"]
        reasons.append(f"Rate applying: half-hourly prices averaging {rs['average_p_kwh']}p/kWh (last 7 days ranged {lo}p to {hi}p); standing charge {rs['standing_charge_p_day']}p/day.")
    elif rs["cheapest_window_local"]:
        reasons.append(f"Rate applying: {rs['min_p_kwh']}p/kWh in {rs['cheapest_window_local']} local time, up to {rs['max_p_kwh']}p/kWh at peak; standing charge {rs['standing_charge_p_day']}p/day.")
    else:
        reasons.append(f"Rate applying: a flat {rs['average_p_kwh']}p/kWh with a standing charge of {rs['standing_charge_p_day']}p/day.")
    if profile.has_ev and bd.ev_avg_p_kwh is not None:
        pattern = profile.ev_charging_pattern or "flexible"
        msg = f"EV charging ({bd.ev_kwh:,.0f} kWh/yr, {pattern}) averages {bd.ev_avg_p_kwh:.1f}p/kWh on this tariff"
        if baseline and baseline[0] is not tariff and baseline[1].ev_avg_p_kwh is not None:
            msg += f" against {baseline[1].ev_avg_p_kwh:.1f}p on {baseline[0].name}"
        reasons.append(msg + ".")
        if pattern == "daytime" and rs["cheapest_window_local"]:
            caveats.append("Daytime EV charging does not use this tariff's cheap window, so most charging is billed at higher rates.")
    if profile.has_solar:
        via = f" and {bd.battery_from_solar_kwh:,.0f} kWh via the battery" if profile.has_battery else ""
        reasons.append(f"Solar (about {bd.solar_generated_kwh:,.0f} kWh/yr) supplies {bd.solar_self_used_kwh:,.0f} kWh directly{via}; {bd.solar_exported_kwh:,.0f} kWh is exported.")
    if profile.has_battery:
        if profile.battery_can_shift_to_offpeak:
            if bd.battery_shifted_kwh > 0:
                reasons.append(f"Battery shifts about {bd.battery_shifted_kwh:,.0f} kWh/yr from cheap to expensive slots, which is why a time-of-use rate helps.")
            elif max(tariff.slot_rates_p_kwh) * ASSUMPTIONS["battery_round_trip_efficiency"] > min(tariff.slot_rates_p_kwh):
                reasons.append("Battery could shift load, but solar, the battery and cheap-slot charging already cover most peak-time demand, leaving little to shift.")
            else:
                reasons.append("Battery could shift load, but the gap between cheap and peak rates here is too small to profit after round-trip losses.")
        else:
            caveats.append("Battery cannot charge from the grid at off-peak times, so it gets no arbitrage credit and only stores solar.")
    if baseline and baseline[0] is not tariff:
        base_tariff, base_bd = baseline
        diff = (base_bd.import_cost_gbp + base_bd.standing_charge_gbp) - (bd.import_cost_gbp + bd.standing_charge_gbp)
        word = "cheaper" if diff >= 0 else "dearer"
        reasons.append(f"Estimated £{abs(diff):,.0f}/yr {word} than {base_tariff.name} for this profile.")
        if tariff.family in ("go", "cosy") and rs["max_p_kwh"] > max(base_tariff.slot_rates_p_kwh):
            caveats.append(f"Peak rate ({rs['max_p_kwh']}p) is higher than {base_tariff.name}'s, so usage outside the cheap windows costs more.")
    if tariff.family == "agile":
        caveats.append("Agile prices change every half hour and can spike in the early evening; this estimate uses a 7-day average and suits households that can shift load.")
    return reasons, caveats


def _recommendation(role: str, rank: int, tariff: Tariff, reasons: list[str], caveats: list[str], **money: float) -> dict[str, Any]:
    return {
        "role": role, "rank": rank, "family": tariff.family, "name": tariff.name,
        "product_code": tariff.product_code, "tariff_code": tariff.tariff_code,
        **{k: round(v, 2) for k, v in money.items()},
        "rates": _rates_summary(tariff), "reasons": reasons, "caveats": caveats,
        "source_urls": tariff.source_urls,
    }


def build_recommendations(profile: Profile, market: Market) -> dict[str, Any]:
    """Pure recommendation logic over a Profile and a Market. No I/O."""
    archetype = classify_profile(profile)
    breakdowns = {t.family: _cost_for_tariff(profile, t) for t in market.import_tariffs}
    total = lambda t: breakdowns[t.family].import_cost_gbp + breakdowns[t.family].standing_charge_gbp
    ranked = sorted(market.import_tariffs, key=total)
    baseline_tariff = next((t for t in market.import_tariffs if t.family == "variable"), None)
    baseline = (baseline_tariff, breakdowns["variable"]) if baseline_tariff else None

    notes: list[str] = []
    if ranked[0].family == "agile" and not _can_shift_load(profile) and len(ranked) > 1:
        agile = ranked.pop(0)
        ranked.insert(1, agile)
        notes.append("Agile has the lowest average-price estimate but is not ranked first: nothing in this profile (flexible EV charging or a battery that shifts to off-peak) lets you avoid its evening peaks.")

    export_ranked: list[tuple[Tariff, float]] = []
    export_vec = breakdowns[ranked[0].family].export_by_slot_kwh
    if profile.has_solar and sum(export_vec) < MIN_EXPORT_KWH:
        notes.append(f"Under {MIN_EXPORT_KWH:.0f} kWh/yr is left to export (household demand and the battery absorb nearly all solar), so no export tariff is ranked. An export tariff would still pay for any occasional surplus.")
    elif profile.has_solar and market.export_tariffs:
        export_ranked = sorted(
            ((t, sum(k * r for k, r in zip(export_vec, t.slot_rates_p_kwh)) / 100) for t in market.export_tariffs),
            key=lambda pair: -pair[1],
        )

    items: list[dict[str, Any]] = []
    for rank, tariff in enumerate(ranked, start=1):
        bd = breakdowns[tariff.family]
        reasons, caveats = _import_reasons(profile, tariff, bd, baseline)
        items.append(_recommendation("import", rank, tariff, reasons, caveats, est_annual_cost_gbp=total(tariff)))
    for rank, (tariff, income) in enumerate(export_ranked, start=1):
        exported = sum(breakdowns[ranked[0].family].export_by_slot_kwh)
        reasons = [f"Rate applying: {_rates_summary(tariff)['average_p_kwh']}p/kWh on average for about {exported:,.0f} kWh/yr of exports (from {ranked[0].name} dispatch)."]
        caveats = ["Assumes exports are not time-shifted, so half-hourly export tariffs may pay more for a household that can time exports."] if tariff.family == "agile_outgoing" else []
        items.append(_recommendation("export", rank, tariff, reasons, caveats, est_annual_income_gbp=income))

    top_import = next(i for i in items if i["role"] == "import")
    top_export = next((i for i in items if i["role"] == "export"), None)
    summary = f"Best fit: {top_import['name']}"
    net = top_import["est_annual_cost_gbp"]
    if top_export:
        summary += f" + {top_export['name']}"
        net -= top_export["est_annual_income_gbp"]
    summary += f" (estimated net electricity cost about £{net:,.0f}/yr)."

    return {
        "archetype": archetype,
        "archetype_title": ARCHETYPES[archetype]["title"],
        "archetype_explanation": ARCHETYPES[archetype]["explanation"],
        "summary": summary,
        "recommendations": [i for i in (top_import, top_export) if i],
        "alternatives": [i for i in items if i is not top_import and i is not top_export],
        "notes": notes,
        "warnings": list(profile.warnings),
        "assumptions": dict(ASSUMPTIONS),
        "data_source": {
            "api": API_BASE, "fetched_at": market.fetched_at,
            "region": f"_{market.region}", "region_name": REGIONS[market.region],
            "unavailable_products": market.unavailable,
        },
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# 5. Public entry point
# ---------------------------------------------------------------------------

def _as_bool(data: dict[str, Any], key: str, required: bool = False) -> bool:
    if key not in data or data[key] is None:
        if required:
            raise ProfileError(f"{key} is required (true or false)")
        return False
    if not isinstance(data[key], bool):
        raise ProfileError(f"{key} must be true or false, got {data[key]!r}")
    return data[key]


def _as_number(data: dict[str, Any], key: str, required: bool = False, positive: bool = True) -> Optional[float]:
    if key not in data or data[key] is None:
        if required:
            raise ProfileError(f"{key} is required")
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{key} must be a number, got {value!r}")
    if positive and value <= 0:
        raise ProfileError(f"{key} must be greater than zero")
    return float(value)


def validate_profile(data: dict[str, Any]) -> Profile:
    """Turn a raw profile dict into a validated Profile. Raises ProfileError."""
    if not isinstance(data, dict):
        raise ProfileError("profile must be a dict")
    region_raw = data.get("region")
    if region_raw is None or str(region_raw).strip() == "":
        raise ProfileError(f"region is required. Valid GSP letters: {', '.join(f'{k} ({v})' for k, v in REGIONS.items())}")
    region = str(region_raw).strip().upper().lstrip("_")
    if region not in REGIONS:
        raise ProfileError(f"region {region_raw!r} is not valid. Valid GSP letters: {', '.join(f'{k} ({v})' for k, v in REGIONS.items())}")

    profile = Profile(
        region=region,
        average_usage_kwh=_as_number(data, "average_usage_kwh", required=True),  # type: ignore[arg-type]
        has_solar=_as_bool(data, "has_solar", required=True),
        has_ev=_as_bool(data, "has_ev", required=True),
        export_capacity_kw=_as_number(data, "export_capacity_kw"),
        has_battery=_as_bool(data, "has_battery"),
        battery_kwh=_as_number(data, "battery_kwh"),
        battery_can_shift_to_offpeak=_as_bool(data, "battery_can_shift_to_offpeak"),
        ev_kwh_per_week=_as_number(data, "ev_kwh_per_week"),
    )
    if profile.has_ev:
        pattern = data.get("ev_charging_pattern")
        if pattern not in ("overnight", "daytime", "flexible"):
            raise ProfileError(f"ev_charging_pattern must be 'overnight', 'daytime' or 'flexible' when has_ev is true, got {pattern!r}")
        profile.ev_charging_pattern = pattern
        ev_year = (profile.ev_kwh_per_week or ASSUMPTIONS["ev_default_kwh_per_week"]) * 52
        if ev_year > profile.average_usage_kwh * ASSUMPTIONS["ev_max_share_of_usage"]:
            profile.warnings.append(f"EV charging ({ev_year:,.0f} kWh/yr) is more than {ASSUMPTIONS['ev_max_share_of_usage']:.0%} of average_usage_kwh, so it was capped. average_usage_kwh should be the household total including EV charging.")
        if profile.ev_kwh_per_week is None:
            profile.warnings.append(f"ev_kwh_per_week not given; assumed {ASSUMPTIONS['ev_default_kwh_per_week']:.0f} kWh/week.")
    if profile.has_solar and profile.export_capacity_kw is None:
        profile.warnings.append(f"export_capacity_kw not given; assumed a {ASSUMPTIONS['solar_default_kwp']} kWp array.")
    if profile.has_battery:
        if profile.battery_kwh is None:
            profile.warnings.append(f"battery_kwh not given; assumed {ASSUMPTIONS['battery_default_kwh']} kWh.")
        if "battery_can_shift_to_offpeak" not in data:
            profile.warnings.append("battery_can_shift_to_offpeak not given; assumed false, so the battery only stores solar.")
    if profile.has_battery and not profile.has_solar and not profile.battery_can_shift_to_offpeak:
        profile.warnings.append("A battery that cannot charge from the grid at off-peak times and has no solar gives no tariff advantage, so it is ignored.")
    return profile


def recommend_tariff(profile: dict[str, Any], *, market: Optional[Market] = None,
                     fetcher: Callable[[str], Market] = fetch_market) -> dict[str, Any]:
    """Recommend Octopus tariffs for a household profile.

    profile keys:
        region (required)              GSP letter A-P, with or without a leading underscore
        average_usage_kwh (required)   annual household electricity use, including EV charging
        has_solar (required)           bool
        has_ev (required)              bool
        export_capacity_kw             solar array size in kW, if known
        ev_charging_pattern            'overnight' | 'daytime' | 'flexible' (required if has_ev)
        ev_kwh_per_week                rough EV charging energy
        has_battery, battery_kwh, battery_can_shift_to_offpeak

    Returns a JSON-serialisable dict (see build_recommendations). Pass `market`
    to skip the network (tests, caching); `fetcher` swaps the API layer.
    Raises ProfileError for bad input and OctopusApiError for network problems.
    """
    validated = validate_profile(profile)
    return build_recommendations(validated, market or fetcher(validated.region))
