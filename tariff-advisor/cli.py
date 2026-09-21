"""Command-line interface for the Octopus Tariff Advisor.

A thin wrapper: parse arguments into a profile dict, call
core.recommend_tariff(), and print the result. All tariff logic and API calls
live in core.py. Informational only; not financial or regulated switching advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import core


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Explore which Octopus Energy tariffs fit a household. Informational only; not financial or regulated switching advice.",
    )
    p.add_argument("--config", help="JSON file containing a profile; command-line flags override it")
    p.add_argument("--region", help="GSP region letter A-P (required, here or in --config)")
    p.add_argument("--usage-kwh", type=float, help="annual electricity use in kWh, including EV charging (required)")
    p.add_argument("--solar", action="store_true", default=None, help="household has solar panels")
    p.add_argument("--export-kw", type=float, help="solar array / export capacity in kW, if known")
    p.add_argument("--ev", action="store_true", default=None, help="household has an EV")
    p.add_argument("--ev-pattern", choices=["overnight", "mixed", "daytime", "flexible"], help="how the EV is charged (overnight = off-peak only; mixed = off-peak and peak)")
    p.add_argument("--ev-kwh-per-week", type=float, help="rough EV charging energy per week")
    p.add_argument("--battery", action="store_true", default=None, help="household has home battery storage")
    p.add_argument("--battery-kwh", type=float, help="battery capacity in kWh")
    p.add_argument("--battery-shifts", action="store_true", default=None, help="battery can charge at off-peak times and discharge at peak")
    p.add_argument("--json", action="store_true", help="print the raw result as JSON")
    return p


def profile_from_args(args: argparse.Namespace) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            profile.update(json.load(fh))
    flags = {
        "region": args.region,
        "average_usage_kwh": args.usage_kwh,
        "has_solar": args.solar,
        "export_capacity_kw": args.export_kw,
        "has_ev": args.ev,
        "ev_charging_pattern": args.ev_pattern,
        "ev_kwh_per_week": args.ev_kwh_per_week,
        "has_battery": args.battery,
        "battery_kwh": args.battery_kwh,
        "battery_can_shift_to_offpeak": args.battery_shifts,
    }
    profile.update({k: v for k, v in flags.items() if v is not None})
    # Unspecified yes/no questions mean "no" at the command line.
    profile.setdefault("has_solar", False)
    profile.setdefault("has_ev", False)
    return profile


def render(result: dict[str, Any]) -> str:
    out: list[str] = []
    src = result["data_source"]
    out.append(f"{result['archetype_title']}  [{src['region_name']}, region {src['region']}]")
    out.append(result["archetype_explanation"])
    out.append("")
    out.append(result["summary"])
    for w in result["warnings"] + result["notes"]:
        out.append(f"  ! {w}")

    def block(item: dict[str, Any]) -> None:
        label = "Import" if item["role"] == "import" else "Export"
        money = (f"est. £{item['est_annual_cost_gbp']:,.0f}/yr" if item["role"] == "import"
                 else f"est. £{item['est_annual_income_gbp']:,.0f}/yr income")
        out.append(f"  {item['rank']}. {item['name']} ({label}, {money})")
        for r in item["reasons"]:
            out.append(f"       - {r}")
        for c in item["caveats"]:
            out.append(f"       ! {c}")

    out.append("")
    out.append("Recommended")
    for item in result["recommendations"]:
        block(item)
    if result["alternatives"]:
        out.append("")
        out.append("Alternatives")
        for item in result["alternatives"]:
            block(item)
    if src["unavailable_products"]:
        out.append("")
        out.append("Not available for this region: " + ", ".join(src["unavailable_products"]))
    if result["assumed_inputs"]:
        out.append("")
        out.append("This estimate assumes the following. To improve it, provide:")
        for a in result["assumed_inputs"]:
            out.append(f"  - {a['question']}  (assumed: {a['assumed']})")
    out.append("")
    out.append("Key assumptions (all estimates depend on these):")
    for key, value in result["assumptions"].items():
        out.append(f"  - {key.replace('_', ' ')}: {value}")
    out.append("")
    out.append(f"Rates fetched {src['fetched_at']} from {src['api']}")
    out.append(result["disclaimer"])
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = core.recommend_tariff(profile_from_args(args))
    except core.ProfileError as exc:
        print(f"Profile problem: {exc}", file=sys.stderr)
        return 2
    except core.OctopusApiError as exc:
        print(f"Could not get Octopus rates: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read the config file: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
