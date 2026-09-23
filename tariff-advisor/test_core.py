"""Offline tests for core.py. No network: every test uses a hand-built Market.

Run from this folder:  python -m unittest -v test_core

The synthetic market below uses rates modelled on real Octopus tariffs, but
the tests assert on structure and relationships (which tariff wins, what is
flagged), not on live prices, so they stay stable as real rates change.
"""

import ast
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core
from core import Market, Tariff


def slots(default, spans):
    """A 48-slot rate list: `default` everywhere, overridden by {(start, end): rate}."""
    rates = [default] * 48
    for (a, b), value in spans.items():
        for s in range(a, b):
            rates[s] = value
    return rates


def tariff(family, direction, name, standing, rates):
    return Tariff(family, direction, name, family.upper(), "T-" + family, standing, rates, "test", ["test-url"])


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


AGILE_RATES = [12 + (25 if 32 <= s < 38 else 0) + (-5 if 4 <= s < 12 else 0) for s in range(48)]  # evening spike, cheap night


def make_market():
    return Market(
        "C", "2026-01-01T00:00:00+00:00",
        [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("go", "IMPORT", "Octopus Go", 44.1, slots(30.99, {(1, 11): 8.63})),
            tariff("cosy", "IMPORT", "Cosy Octopus", 42.4, slots(26.6, {(8, 14): 13.07, (26, 32): 13.07, (44, 48): 13.07, (32, 38): 39.95})),
            tariff("agile", "IMPORT", "Agile Octopus", 39.5, AGILE_RATES),
            tariff("intelligent_go", "IMPORT", "Intelligent Octopus Go", 44.5, slots(31.0, {(1, 11): 7.2})),
        ],
        [
            tariff("outgoing", "EXPORT", "Outgoing Octopus", 0, [12.0] * 48),
            tariff("agile_outgoing", "EXPORT", "Agile Outgoing", 0, [12.8] * 48),
        ],
    )


BASE = {"region": "C", "average_usage_kwh": 4000}

ARCHETYPE_PROFILES = {
    "neither": dict(has_solar=False, has_ev=False),
    "solar_only": dict(has_solar=True, has_ev=False, export_capacity_kw=4),
    "ev_overnight": dict(has_solar=False, has_ev=True, ev_charging_pattern="overnight"),
    "ev_daytime_flexible": dict(has_solar=False, has_ev=True, ev_charging_pattern="daytime"),
    "solar_ev": dict(has_solar=True, has_ev=True, ev_charging_pattern="overnight", export_capacity_kw=4),
    "solar_battery": dict(has_solar=True, has_ev=False, has_battery=True, battery_kwh=5, battery_can_shift_to_offpeak=True, export_capacity_kw=4),
    "solar_battery_ev": dict(has_solar=True, has_ev=True, ev_charging_pattern="overnight", has_battery=True, battery_kwh=10, battery_can_shift_to_offpeak=True, export_capacity_kw=6),
    "battery_only": dict(has_solar=False, has_ev=False, has_battery=True, battery_kwh=10, battery_can_shift_to_offpeak=True),
}


def run(extra, market=None):
    return core.recommend_tariff({**BASE, **extra}, market=market or make_market())


class ArchetypeTests(unittest.TestCase):
    def test_each_profile_classifies_to_its_archetype(self):
        for expected, extra in ARCHETYPE_PROFILES.items():
            with self.subTest(expected):
                self.assertEqual(run(extra)["archetype"], expected)

    def test_result_is_plain_json_serialisable_data(self):
        for name, extra in ARCHETYPE_PROFILES.items():
            with self.subTest(name):
                result = run(extra)
                self.assertEqual(json.loads(json.dumps(result)), result)

    def test_result_has_documented_keys(self):
        result = run(ARCHETYPE_PROFILES["solar_ev"])
        for key in ("archetype", "summary", "recommendations", "alternatives", "notes", "warnings", "assumptions", "data_source", "disclaimer"):
            self.assertIn(key, result)
        self.assertIn("not financial advice", result["disclaimer"].lower())

    def test_battery_that_cannot_shift_and_no_solar_is_treated_as_neither(self):
        result = run(dict(has_solar=False, has_ev=False, has_battery=True, battery_can_shift_to_offpeak=False))
        self.assertEqual(result["archetype"], "neither")
        self.assertTrue(any("no tariff advantage" in w for w in result["warnings"]))

    def test_battery_with_ev_but_no_solar_classifies_by_ev_pattern(self):
        result = run(dict(has_solar=False, has_ev=True, ev_charging_pattern="overnight", has_battery=True, battery_can_shift_to_offpeak=True))
        self.assertEqual(result["archetype"], "ev_overnight")


class RecommendationTests(unittest.TestCase):
    def top(self, result, role="import"):
        return next(r for r in result["recommendations"] if r["role"] == role)

    def test_overnight_ev_picks_the_cheapest_offpeak_tariff_and_prices_ev_at_offpeak(self):
        result = run(ARCHETYPE_PROFILES["ev_overnight"])
        self.assertEqual(self.top(result)["family"], "intelligent_go")  # its off-peak rate undercuts Go's
        self.assertTrue(any("averages 7.2p/kWh" in r for r in self.top(result)["reasons"]))

    def test_rank_is_by_estimated_cost(self):
        result = run(ARCHETYPE_PROFILES["ev_overnight"])
        imports = [r for r in result["recommendations"] + result["alternatives"] if r["role"] == "import"]
        imports.sort(key=lambda r: r["rank"])
        costs = [r["est_annual_cost_gbp"] for r in imports]
        agile_demoted = any("Agile" in n for n in result["notes"])
        if not agile_demoted:
            self.assertEqual(costs, sorted(costs))
        self.assertEqual(imports[0]["rank"], 1)

    def test_solar_household_gets_an_export_recommendation(self):
        result = run(ARCHETYPE_PROFILES["solar_ev"])
        self.assertIn("export", [r["role"] for r in result["recommendations"]])
        self.assertIn("est_annual_income_gbp", self.top(result, "export"))

    def test_non_solar_household_gets_no_export_recommendation(self):
        result = run(ARCHETYPE_PROFILES["ev_overnight"])
        self.assertNotIn("export", [r["role"] for r in result["recommendations"] + result["alternatives"]])

    def test_export_not_ranked_when_almost_nothing_is_left_to_export(self):
        result = run(ARCHETYPE_PROFILES["solar_battery"])
        self.assertNotIn("export", [r["role"] for r in result["recommendations"]])
        self.assertTrue(any("export tariff" in n for n in result["notes"]))

    def test_agile_is_demoted_when_profile_cannot_shift_load(self):
        cheap_agile = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("agile", "IMPORT", "Agile Octopus", 39.5, [5.0] * 48),
        ], [])
        result = run(dict(has_solar=False, has_ev=False), market=cheap_agile)
        self.assertEqual(self.top(result)["family"], "variable")
        self.assertTrue(any("Agile" in n for n in result["notes"]))

    def test_agile_can_rank_first_when_profile_can_shift_load(self):
        cheap_agile = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("agile", "IMPORT", "Agile Octopus", 39.5, [5.0] * 48),
        ], [])
        result = run(dict(has_solar=False, has_ev=True, ev_charging_pattern="flexible"), market=cheap_agile)
        self.assertEqual(self.top(result)["family"], "agile")
        self.assertFalse(any("not ranked first" in n for n in result["notes"]))

    def test_agile_always_carries_a_volatility_caveat(self):
        result = run(ARCHETYPE_PROFILES["solar_battery_ev"])
        agile = next(r for r in result["recommendations"] + result["alternatives"] if r["family"] == "agile")
        self.assertTrue(any("half hour" in c for c in agile["caveats"]))

    def test_neither_no_longer_leads_with_a_wafer_thin_time_of_use_saving(self):
        """Regression: a household with no solar, EV or battery has no way to earn a
        time-of-use tariff's cheap windows on purpose, so a saving too thin to be more
        than noise from the illustrative demand shape must not be the headline pick."""
        result = run(ARCHETYPE_PROFILES["neither"])
        self.assertEqual(self.top(result)["family"], "variable")
        cosy = next(a for a in result["alternatives"] if a["family"] == "cosy")
        self.assertLess(cosy["est_annual_cost_gbp"], self.top(result)["est_annual_cost_gbp"])  # still nominally cheaper...
        self.assertTrue(any("Cosy" in n and "steadier default" in n for n in result["notes"]))  # ...but demoted, with a stated reason

    def test_thin_margin_promotes_flexible_for_neither_with_a_note(self):
        thin = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("go", "IMPORT", "Octopus Go", 41.5, [26.05] * 48),  # 0.995% cheaper: below the 3% threshold
        ], [])
        result = run(dict(has_solar=False, has_ev=False), market=thin)
        self.assertEqual(self.top(result)["family"], "variable")
        self.assertTrue(any("Go" in n and "steadier default" in n for n in result["notes"]))
        # the edged-out tariff is kept as the very next alternative, not buried
        self.assertEqual(result["alternatives"][0]["family"], "go")

    def test_large_margin_is_kept_for_neither_with_no_note(self):
        large = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("go", "IMPORT", "Octopus Go", 41.5, [20.0] * 48),  # about 21% cheaper: a real structural saving
        ], [])
        result = run(dict(has_solar=False, has_ev=False), market=large)
        self.assertEqual(self.top(result)["family"], "go")
        self.assertFalse(any("steadier default" in n for n in result["notes"]))

    def test_thin_margin_rule_is_specific_to_the_neither_archetype(self):
        """The same thin margin must not promote Flexible for a household that DOES have
        a lever (here, an EV) - the rule only applies when there is no way to earn the
        saving on purpose."""
        thin = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("go", "IMPORT", "Octopus Go", 41.5, [26.05] * 48),
        ], [])
        result = run(dict(has_solar=False, has_ev=True, ev_charging_pattern="overnight"), market=thin)
        self.assertEqual(self.top(result)["family"], "go")
        self.assertFalse(any("steadier default" in n for n in result["notes"]))

    def test_thin_margin_rule_stacks_correctly_after_agile_demotion(self):
        """When Agile is cheapest but gets demoted, the tariff that becomes the new
        leader is itself checked against the same materiality threshold."""
        stacked = Market("C", "x", [
            tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48),
            tariff("agile", "IMPORT", "Agile Octopus", 39.5, [15.0] * 48),  # cheapest raw, but demoted (can't shift)
            tariff("go", "IMPORT", "Octopus Go", 41.5, [26.05] * 48),  # becomes leader after demotion, itself too thin
        ], [])
        result = run(dict(has_solar=False, has_ev=False), market=stacked)
        self.assertEqual(self.top(result)["family"], "variable")
        families_in_order = [self.top(result)["family"]] + [a["family"] for a in result["alternatives"] if a["role"] == "import"]
        self.assertEqual(families_in_order, ["variable", "go", "agile"])
        self.assertEqual(len(result["notes"]), 2)  # both the Agile note and the materiality note fire

    def test_battery_shifting_moves_energy_and_lowers_cost(self):
        with_shift = dict(has_solar=False, has_ev=False, has_battery=True, battery_kwh=10, battery_can_shift_to_offpeak=True)
        without = {**with_shift, "battery_can_shift_to_offpeak": False}
        cost_with = next(r for r in run(with_shift)["alternatives"] + run(with_shift)["recommendations"] if r["family"] == "go")["est_annual_cost_gbp"]
        cost_without = next(r for r in run(without)["alternatives"] + run(without)["recommendations"] if r["family"] == "go")["est_annual_cost_gbp"]
        self.assertLess(cost_with, cost_without)

    def test_daytime_ev_gets_cheap_window_caveat_on_go(self):
        result = run(ARCHETYPE_PROFILES["ev_daytime_flexible"])
        go = next(r for r in result["recommendations"] + result["alternatives"] if r["family"] == "go")
        self.assertTrue(any("cheap window" in c for c in go["caveats"]))

    def test_more_usage_costs_more(self):
        low = run(dict(has_solar=False, has_ev=False, average_usage_kwh=2000))
        high = run(dict(has_solar=False, has_ev=False, average_usage_kwh=6000))
        self.assertLess(low["recommendations"][0]["est_annual_cost_gbp"], high["recommendations"][0]["est_annual_cost_gbp"])

    def test_injected_market_means_the_network_is_never_used(self):
        def boom(region):
            raise AssertionError("fetcher must not be called when a market is supplied")
        core.recommend_tariff({**BASE, **ARCHETYPE_PROFILES["neither"]}, market=make_market(), fetcher=boom)

    def test_fetcher_receives_normalised_region(self):
        seen = []
        def fake(region):
            seen.append(region)
            return make_market()
        core.recommend_tariff({**BASE, "region": "_c", **ARCHETYPE_PROFILES["neither"]}, fetcher=fake)
        self.assertEqual(seen, ["C"])


class ValidationTests(unittest.TestCase):
    def assertRejected(self, profile, fragment):
        with self.assertRaises(core.ProfileError) as ctx:
            core.recommend_tariff(profile, market=make_market())
        self.assertIn(fragment, str(ctx.exception))

    def test_region_is_required_and_error_lists_valid_letters(self):
        self.assertRejected({"average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, "region is required")
        self.assertRejected({"region": "", "average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, "Valid GSP letters")

    def test_invalid_region_rejected(self):
        self.assertRejected({"region": "Z", "average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, "not valid")

    def test_region_accepts_lowercase_and_underscore(self):
        for value in ("c", "_C", " c "):
            with self.subTest(value):
                result = core.recommend_tariff({"region": value, "average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, market=make_market())
                self.assertEqual(result["data_source"]["region"], "_C")

    def test_required_fields(self):
        self.assertRejected({"region": "C", "has_solar": False, "has_ev": False}, "average_usage_kwh is required")
        self.assertRejected({"region": "C", "average_usage_kwh": 3000, "has_ev": False}, "has_solar is required")
        self.assertRejected({"region": "C", "average_usage_kwh": 3000, "has_solar": False}, "has_ev is required")

    def test_ev_needs_a_valid_pattern(self):
        self.assertRejected({**BASE, "has_solar": False, "has_ev": True}, "ev_charging_pattern")
        self.assertRejected({**BASE, "has_solar": False, "has_ev": True, "ev_charging_pattern": "sometimes"}, "ev_charging_pattern")

    def test_type_and_range_errors_name_the_field(self):
        self.assertRejected({**BASE, "has_solar": "yes", "has_ev": False}, "has_solar must be true or false")
        self.assertRejected({"region": "C", "average_usage_kwh": -5, "has_solar": False, "has_ev": False}, "average_usage_kwh must be greater than zero")
        self.assertRejected({"region": "C", "average_usage_kwh": "lots", "has_solar": False, "has_ev": False}, "average_usage_kwh must be a number")
        self.assertRejected({"region": "C", "average_usage_kwh": True, "has_solar": False, "has_ev": False}, "average_usage_kwh must be a number")

    def test_non_dict_profile_rejected(self):
        with self.assertRaises(core.ProfileError):
            core.recommend_tariff("not a dict", market=make_market())

    def test_missing_optional_details_produce_warnings_not_errors(self):
        result = run(dict(has_solar=True, has_ev=True, ev_charging_pattern="overnight", has_battery=True))
        text = " ".join(result["warnings"])
        for fragment in ("export_capacity_kw not given", "ev_kwh_per_week not given", "battery_kwh not given", "battery_can_shift_to_offpeak not given"):
            self.assertIn(fragment, text)

    def test_oversized_ev_is_capped_with_a_warning(self):
        result = run(dict(has_solar=False, has_ev=True, ev_charging_pattern="overnight", ev_kwh_per_week=500, average_usage_kwh=3000))
        self.assertTrue(any("capped" in w for w in result["warnings"]))


class TimeAndRateHelperTests(unittest.TestCase):
    def test_uk_offset_at_bst_boundaries_2026(self):
        # Clocks go forward 01:00 UTC on 29 Mar 2026 and back 01:00 UTC on 25 Oct 2026.
        self.assertEqual(core._uk_offset(utc(2026, 1, 15, 12)), timedelta(0))
        self.assertEqual(core._uk_offset(utc(2026, 3, 29, 0, 59)), timedelta(0))
        self.assertEqual(core._uk_offset(utc(2026, 3, 29, 1, 0)), timedelta(hours=1))
        self.assertEqual(core._uk_offset(utc(2026, 7, 1, 12)), timedelta(hours=1))
        self.assertEqual(core._uk_offset(utc(2026, 10, 25, 0, 59)), timedelta(hours=1))
        self.assertEqual(core._uk_offset(utc(2026, 10, 25, 1, 0)), timedelta(0))

    def test_local_slot_shifts_with_bst(self):
        self.assertEqual(core._local_slot(utc(2026, 1, 15, 0, 0)), 0)
        self.assertEqual(core._local_slot(utc(2026, 7, 1, 0, 0)), 2)  # 01:00 local
        self.assertEqual(core._local_slot(utc(2026, 7, 1, 23, 30)), 1)  # 00:30 local next day

    def test_slot_rates_expand_records_and_handle_open_ended_rate(self):
        start = utc(2026, 1, 15)
        records = [
            {"value_inc_vat": 10.0, "valid_from": "2026-01-15T00:00:00Z", "valid_to": "2026-01-15T05:00:00Z", "payment_method": None},
            {"value_inc_vat": 20.0, "valid_from": "2026-01-15T05:00:00Z", "valid_to": None, "payment_method": None},
        ]
        rates, raw = core._slot_rates_from_records(records, start, start + timedelta(hours=24))
        self.assertEqual(rates[:10], [10.0] * 10)
        self.assertEqual(rates[10:], [20.0] * 38)
        self.assertEqual(len(raw), 48)

    def test_slot_rates_ignore_non_direct_debit_records(self):
        start = utc(2026, 1, 15)
        records = [
            {"value_inc_vat": 99.0, "valid_from": "2026-01-14T00:00:00Z", "valid_to": None, "payment_method": "NON_DIRECT_DEBIT"},
            {"value_inc_vat": 10.0, "valid_from": "2026-01-14T00:00:00Z", "valid_to": None, "payment_method": "DIRECT_DEBIT"},
        ]
        rates, _ = core._slot_rates_from_records(records, start, start + timedelta(hours=24))
        self.assertEqual(set(rates), {10.0})

    def test_slot_rates_map_utc_records_to_local_time_in_summer(self):
        start = utc(2026, 7, 1)
        records = [{"value_inc_vat": 8.0, "valid_from": "2026-06-30T23:30:00Z", "valid_to": "2026-07-01T04:30:00Z", "payment_method": None},
                   {"value_inc_vat": 30.0, "valid_from": "2026-07-01T04:30:00Z", "valid_to": None, "payment_method": None}]
        rates, _ = core._slot_rates_from_records(records, start, start + timedelta(hours=24))
        self.assertEqual(core._cheapest_window(rates), "01:00-05:30")  # 00:00-04:30 UTC seen from a 00:00 UTC start

    def test_slot_rates_with_no_data_raise_api_error(self):
        with self.assertRaises(core.OctopusApiError):
            core._slot_rates_from_records([], utc(2026, 1, 15), utc(2026, 1, 16))

    def test_cheapest_window(self):
        self.assertEqual(core._cheapest_window(slots(30, {(1, 11): 8})), "00:30-05:30")
        self.assertIsNone(core._cheapest_window([5.0] * 48))
        self.assertEqual(core._cheapest_window(slots(30, {(0, 4): 8, (44, 48): 8})), "22:00-02:00")  # wraps midnight


class ArchitectureTests(unittest.TestCase):
    """core.py must stay interface-agnostic so an MCP server can wrap it unchanged."""

    def setUp(self):
        self.tree = ast.parse((Path(__file__).parent / "core.py").read_text(encoding="utf-8"))

    def test_core_does_not_import_ui_modules(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"argparse", "sys", "click", "cli"}, imported)

    def test_core_never_prints_prompts_or_exits(self):
        called = {n.func.id for n in ast.walk(self.tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(called & {"print", "input", "exit", "quit"}, called)


def assume(profile, market=None):
    return core.recommend_tariff(profile, market=market or make_market(), allow_assumptions=True)


def inputs_of(result):
    return [a["input"] for a in result["assumed_inputs"]]


class AssumptionTests(unittest.TestCase):
    """allow_assumptions=True: answer from whatever was given, and report what was assumed."""

    def test_empty_profile_still_gets_an_answer_from_defaults(self):
        result = assume({})
        self.assertEqual(result["archetype"], "neither")
        self.assertEqual(result["data_source"]["region"], "_C")
        self.assertEqual(result["data_source"]["region_name"], "London")
        self.assertTrue(result["based_on_assumptions"])
        self.assertEqual(inputs_of(result), ["region", "average_usage_kwh", "has_solar", "has_ev", "has_battery"])
        self.assertTrue(result["recommendations"])

    def test_ev_only_prompt_asks_about_everything_it_did_not_say(self):
        result = assume({"has_ev": True})
        self.assertEqual(inputs_of(result), ["region", "average_usage_kwh", "has_solar", "ev_annual_kwh", "ev_charging_pattern", "has_battery"])
        self.assertEqual(result["archetype"], "ev_daytime_flexible")  # the assumed mixed pattern

    def test_assumed_usage_is_a_total_that_includes_the_ev(self):
        with_ev = {a["input"]: a for a in assume({"has_ev": True})["assumed_inputs"]}["average_usage_kwh"]
        self.assertIn("2,500", with_ev["assumed"])
        self.assertIn("2,080", with_ev["assumed"])
        self.assertIn("including annual EV charging", with_ev["question"])
        without_ev = {a["input"]: a for a in assume({})["assumed_inputs"]}["average_usage_kwh"]
        self.assertNotIn("EV", without_ev["assumed"])
        self.assertNotIn("including annual EV charging", without_ev["question"])

    def test_the_questions_use_the_agreed_wording(self):
        everything_missing = {"has_solar": True, "has_ev": True, "has_battery": True}
        questions = {a["input"]: a["question"] for a in assume(everything_missing)["assumed_inputs"]}
        self.assertEqual(questions, {
            "region": "the postcode of the property",
            "average_usage_kwh": "the total annual usage, including annual EV charging consumption",
            "solar_kwp": "the size of the solar array (kWp)",
            "ev_annual_kwh": "the annual EV charging consumption (kWh)",
            "ev_charging_pattern": "the EV charging pattern: off-peak only, or a mix of off-peak and peak",
            "battery_kwh": "the home battery size (kWh)",
            "battery_can_shift_to_offpeak": "whether the battery can charge from the grid at off-peak times and discharge at peak",
        })

    def test_questions_are_reported_in_a_fixed_order(self):
        result = assume({"has_solar": True, "has_ev": True, "has_battery": True})
        self.assertEqual(inputs_of(result), ["region", "average_usage_kwh", "solar_kwp", "ev_annual_kwh", "ev_charging_pattern", "battery_kwh", "battery_can_shift_to_offpeak"])

    def test_inputs_the_user_provided_are_never_listed(self):
        full = {"region": "N", "average_usage_kwh": 5000, "has_solar": True, "solar_kwp": 5, "has_ev": True, "ev_annual_kwh": 2500,
                "ev_charging_pattern": "overnight", "has_battery": True, "battery_kwh": 10, "battery_can_shift_to_offpeak": True}
        result = assume(full)
        self.assertEqual(result["assumed_inputs"], [])
        self.assertFalse(result["based_on_assumptions"])

    def test_each_provided_field_removes_exactly_its_own_question(self):
        base = {"has_solar": True, "has_ev": True, "has_battery": True}
        before = set(inputs_of(assume(base)))
        for extra, gone in [({"region": "H"}, "region"), ({"average_usage_kwh": 4000}, "average_usage_kwh"), ({"solar_kwp": 3.5}, "solar_kwp"),
                            ({"ev_annual_kwh": 2000}, "ev_annual_kwh"), ({"ev_charging_pattern": "overnight"}, "ev_charging_pattern"),
                            ({"battery_kwh": 8}, "battery_kwh"), ({"battery_can_shift_to_offpeak": False}, "battery_can_shift_to_offpeak")]:
            with self.subTest(gone):
                self.assertEqual(before - set(inputs_of(assume({**base, **extra}))), {gone})

    def test_explicit_no_is_an_answer_not_an_assumption(self):
        result = assume({"has_solar": False, "has_ev": False, "has_battery": False})
        self.assertEqual(inputs_of(result), ["region", "average_usage_kwh"])

    def test_features_left_out_are_assumed_absent_and_listed(self):
        result = assume({"region": "C", "average_usage_kwh": 3000})
        self.assertEqual(inputs_of(result), ["has_solar", "has_ev", "has_battery"])
        self.assertEqual(result["archetype"], "neither")

    def test_input_that_is_present_gets_its_follow_up_questions_only_when_present(self):
        no_solar = assume({"has_solar": False})
        self.assertNotIn("solar_kwp", inputs_of(no_solar))
        self.assertIn("solar_kwp", inputs_of(assume({"has_solar": True})))
        self.assertNotIn("ev_charging_pattern", inputs_of(assume({"has_ev": False})))
        self.assertNotIn("battery_kwh", inputs_of(assume({"has_battery": False})))

    def test_assumed_ev_pattern_is_mixed_and_says_so(self):
        entry = next(a for a in assume({"has_ev": True})["assumed_inputs"] if a["input"] == "ev_charging_pattern")
        self.assertIn("mixed", entry["assumed"])
        self.assertIn("70% off-peak", entry["assumed"])

    def test_values_are_still_validated_when_assuming(self):
        for label, profile in {
            "bad region": {"region": "Z"},
            "negative usage": {"average_usage_kwh": -1},
            "bad pattern": {"has_ev": True, "ev_charging_pattern": "sometimes"},
            "string bool": {"has_solar": "yes"},
        }.items():
            with self.subTest(label):
                with self.assertRaises(core.ProfileError):
                    assume(profile)

    def test_invalid_input_fails_before_any_fetch(self):
        def boom(region):
            raise AssertionError("fetcher must not be called for an invalid profile")
        with self.assertRaises(core.ProfileError):
            core.recommend_tariff({"average_usage_kwh": -1}, fetcher=boom, allow_assumptions=True)

    def test_result_stays_json_serialisable(self):
        result = assume({"has_solar": True, "has_ev": True, "has_battery": True})
        self.assertEqual(json.loads(json.dumps(result)), result)
        for entry in result["assumed_inputs"]:
            self.assertEqual(set(entry), {"input", "question", "assumed"})

    def test_strict_mode_is_unchanged_and_still_requires_region(self):
        with self.assertRaises(core.ProfileError):
            core.recommend_tariff({}, market=make_market())
        with self.assertRaises(core.ProfileError):
            core.recommend_tariff({"average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, market=make_market())

    def test_strict_mode_reports_only_optional_details_it_defaulted(self):
        full_neither = core.recommend_tariff({"region": "C", "average_usage_kwh": 3000, "has_solar": False, "has_ev": False}, market=make_market())
        self.assertEqual(full_neither["assumed_inputs"], [])
        self.assertFalse(full_neither["based_on_assumptions"])
        solar_no_size = core.recommend_tariff({"region": "C", "average_usage_kwh": 3000, "has_solar": True, "has_ev": False}, market=make_market())
        self.assertEqual(inputs_of(solar_no_size), ["solar_kwp"])
        self.assertTrue(solar_no_size["based_on_assumptions"])


class ProfileKeyAliasTests(unittest.TestCase):
    SOLAR = {"region": "C", "average_usage_kwh": 4000, "has_solar": True, "has_ev": False}

    def test_solar_kwp_and_export_capacity_kw_mean_the_same(self):
        self.assertEqual(run({**self.SOLAR, "solar_kwp": 5}), run({**self.SOLAR, "export_capacity_kw": 5}))

    def test_conflicting_solar_sizes_are_rejected_but_matching_ones_are_fine(self):
        with self.assertRaises(core.ProfileError):
            run({**self.SOLAR, "solar_kwp": 4, "export_capacity_kw": 6})
        run({**self.SOLAR, "solar_kwp": 4, "export_capacity_kw": 4})

    def test_annual_ev_kwh_equals_the_same_weekly_figure(self):
        ev = {"region": "C", "average_usage_kwh": 5000, "has_solar": False, "has_ev": True, "ev_charging_pattern": "overnight"}
        self.assertEqual(run({**ev, "ev_annual_kwh": 2600}), run({**ev, "ev_kwh_per_week": 50}))

    def test_giving_both_ev_figures_is_rejected(self):
        with self.assertRaises(core.ProfileError):
            run({"region": "C", "average_usage_kwh": 5000, "has_solar": False, "has_ev": True, "ev_charging_pattern": "overnight",
                 "ev_annual_kwh": 2600, "ev_kwh_per_week": 50})

    def test_annual_ev_figure_is_used_and_more_ev_on_the_same_base_costs_more(self):
        flat = tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48)
        base = 3000  # household use excluding the EV, held constant
        costs = {}
        for ev_kwh in (1000, 4000):
            profile = core.validate_profile({"region": "C", "average_usage_kwh": base + ev_kwh, "has_solar": False, "has_ev": True,
                                             "ev_charging_pattern": "daytime", "ev_annual_kwh": ev_kwh})
            breakdown = core._cost_for_tariff(profile, flat)
            self.assertAlmostEqual(breakdown.ev_kwh, ev_kwh)
            costs[ev_kwh] = breakdown.import_cost_gbp
        self.assertLess(costs[1000], costs[4000])


class MixedEvPatternTests(unittest.TestCase):
    GO = slots(30.99, {(1, 11): 8.63})
    OVERNIGHT = set(core._window_slots("23:00-07:00"))
    EVENING = set(core._window_slots("17:00-22:00"))

    def test_mixed_charging_is_split_between_the_cheap_window_and_the_evening(self):
        load = core._place_ev("mixed", 10.0, self.GO)
        self.assertAlmostEqual(sum(load), 10.0)
        self.assertAlmostEqual(sum(load[s] for s in self.EVENING), 3.0)
        self.assertAlmostEqual(sum(v for s, v in enumerate(load) if s in self.OVERNIGHT), 7.0)
        self.assertTrue(all(load[s] == 0 for s in range(48) if s not in self.OVERNIGHT | self.EVENING))

    def test_offpeak_share_lands_in_the_cheapest_slots(self):
        load = core._place_ev("mixed", 10.0, self.GO)
        self.assertAlmostEqual(sum(load[s] for s in range(1, 11)), 7.0)  # all of it in Go's 00:30-05:30 window

    def test_mixed_costs_more_than_overnight_and_less_than_daytime_on_go(self):
        go = tariff("go", "IMPORT", "Octopus Go", 44.1, self.GO)
        price = {}
        for pattern in ("overnight", "mixed", "daytime"):
            profile = core.validate_profile({"region": "C", "average_usage_kwh": 5000, "has_solar": False, "has_ev": True, "ev_charging_pattern": pattern})
            price[pattern] = core._cost_for_tariff(profile, go).ev_avg_p_kwh
        self.assertLess(price["overnight"], price["mixed"])
        self.assertLess(price["mixed"], price["daytime"])

    def test_pattern_makes_no_difference_on_a_flat_tariff(self):
        flat = tariff("variable", "IMPORT", "Flexible Octopus", 41.5, [26.35] * 48)
        prices = {p: core._cost_for_tariff(core.validate_profile({"region": "C", "average_usage_kwh": 5000, "has_solar": False, "has_ev": True, "ev_charging_pattern": p}), flat).ev_avg_p_kwh
                  for p in core.EV_PATTERNS}
        for value in prices.values():
            self.assertAlmostEqual(value, 26.35)

    def test_mixed_is_accepted_and_classified_with_the_non_overnight_ev_archetype(self):
        profile = core.validate_profile({**BASE, "has_solar": False, "has_ev": True, "ev_charging_pattern": "mixed"})
        self.assertEqual(core.classify_profile(profile), "ev_daytime_flexible")

    def test_every_documented_pattern_is_accepted(self):
        for pattern in ("overnight", "mixed", "daytime", "flexible"):
            with self.subTest(pattern):
                core.validate_profile({**BASE, "has_solar": False, "has_ev": True, "ev_charging_pattern": pattern})

    def test_mixed_recommendation_flags_the_peak_share_on_time_of_use_tariffs(self):
        result = run({"has_solar": False, "has_ev": True, "ev_charging_pattern": "mixed"})
        go = next(r for r in result["recommendations"] + result["alternatives"] if r["family"] == "go")
        self.assertTrue(any("Only about 70%" in c for c in go["caveats"]))


class IntelligentGoTests(unittest.TestCase):
    def intelligent_go(self, result):
        return next(r for r in result["recommendations"] + result["alternatives"] if r["family"] == "intelligent_go")

    def test_always_carries_the_fixed_term_and_eligibility_caveat(self):
        result = run(ARCHETYPE_PROFILES["ev_overnight"])
        self.assertTrue(any("12-month fixed-term" in c for c in self.intelligent_go(result)["caveats"]))

    def test_peak_rate_caveat_fires_like_go_and_cosy(self):
        result = run(ARCHETYPE_PROFILES["ev_daytime_flexible"])
        self.assertTrue(any("Peak rate" in c and "higher than" in c for c in self.intelligent_go(result)["caveats"]))


if __name__ == "__main__":
    unittest.main()
