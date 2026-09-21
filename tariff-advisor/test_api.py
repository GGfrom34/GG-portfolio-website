"""Tests for core's API layer against saved Octopus API responses. No network.

core._get_json is replaced with api_fixtures.Playback, which serves responses
recorded from the real API (fixtures/api_responses.json). Because the fixture
was recorded from what core actually requests, these tests exercise real
response shapes: product discovery, per-region tariff lookup, the varying
payment-method key on Flexible Octopus, rate windows and pagination.

Run from this folder:  python -m unittest -v test_api
Refresh the fixture (needs network):  python api_fixtures.py
"""

import copy
import io
import json
import unittest
import urllib.error
import urllib.parse
from datetime import timedelta
from unittest import mock

import api_fixtures
import core
from api_fixtures import NOW, Playback

API = core.API_BASE
PRODUCTS_KEY = f"{API}/products/?brand=OCTOPUS_ENERGY&is_business=false&page_size=100"


def fetch(region="C", stub=None):
    """Run the real fetch_market with core._get_json replaced by a playback stub."""
    stub = stub or Playback.load()
    with mock.patch.object(core, "_get_json", stub):
        return core.fetch_market(region, now=NOW), stub


def by_family(market):
    return {t.family: t for t in market.import_tariffs + market.export_tariffs}


def query(key):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(key).query).items()}


def rate_key(stub, product, region="C"):
    return next(k for k in stub.responses if f"/products/{product}/electricity-tariffs/E-1R-{product}-{region}/standard-unit-rates/" in k)


class DiscoveryAndParsingTests(unittest.TestCase):
    def setUp(self):
        self.market, self.stub = fetch("C")
        self.tariffs = by_family(self.market)

    def test_finds_all_six_families(self):
        self.assertEqual(set(self.tariffs), {"variable", "agile", "go", "cosy", "outgoing", "agile_outgoing"})
        self.assertEqual({t.family for t in self.market.import_tariffs}, {"variable", "agile", "go", "cosy"})
        self.assertEqual({t.family for t in self.market.export_tariffs}, {"outgoing", "agile_outgoing"})
        self.assertEqual(self.market.unavailable, [])

    def test_picks_current_variable_products_not_fixed_or_other_variants(self):
        self.assertEqual(self.tariffs["go"].product_code, "GO-VAR-22-10-14")  # not GO-FIX-12M-* or IOG-*
        self.assertEqual(self.tariffs["cosy"].product_code, "COSY-22-12-08")  # not COSY-FIX-12M-*
        self.assertEqual(self.tariffs["outgoing"].product_code, "OUTGOING-VAR-24-10-26")  # not OUTGOING-PRIME-FIX-*
        self.assertEqual(self.tariffs["agile"].direction, "IMPORT")
        self.assertEqual(self.tariffs["agile_outgoing"].direction, "EXPORT")

    def test_tariff_codes_are_for_the_requested_region(self):
        for tariff in self.tariffs.values():
            self.assertTrue(tariff.tariff_code.endswith("-C"), tariff.tariff_code)
        southern_scotland, _ = fetch("N")
        for tariff in southern_scotland.import_tariffs + southern_scotland.export_tariffs:
            self.assertTrue(tariff.tariff_code.endswith("-N"), tariff.tariff_code)
        self.assertEqual(southern_scotland.region, "N")

    def test_standing_charges_come_from_the_product_detail(self):
        for family, tariff in self.tariffs.items():
            detail = self.stub.responses[f"{API}/products/{tariff.product_code}/"]["single_register_electricity_tariffs"]["_C"]
            entry = next(iter(detail.values()))
            self.assertAlmostEqual(tariff.standing_charge_p_day, entry.get("standing_charge_inc_vat") or 0.0, msg=family)

    def test_flexible_is_found_under_the_varying_payment_key(self):
        detail = self.stub.responses[f"{API}/products/VAR-22-11-01/"]["single_register_electricity_tariffs"]["_C"]
        self.assertEqual(list(detail), ["varying"])  # the recorded shape that broke a hard-coded direct_debit_monthly lookup
        flexible = self.tariffs["variable"]
        self.assertEqual(flexible.tariff_code, detail["varying"]["code"])
        self.assertAlmostEqual(flexible.standing_charge_p_day, detail["varying"]["standing_charge_inc_vat"])

    def test_go_has_two_rates_and_a_local_night_window(self):
        go = self.tariffs["go"]
        self.assertEqual(len(go.slot_rates_p_kwh), 48)
        self.assertEqual(len(set(go.slot_rates_p_kwh)), 2)
        self.assertEqual(core._rates_summary(go)["cheapest_window_local"], "00:30-05:30")  # recorded in BST from UTC 23:30-04:30
        recorded = {r["value_inc_vat"] for r in self.stub.responses[rate_key(self.stub, "GO-VAR-22-10-14")]["results"]}
        self.assertEqual(set(go.slot_rates_p_kwh), recorded)

    def test_cosy_has_three_price_levels(self):
        self.assertEqual(len(set(self.tariffs["cosy"].slot_rates_p_kwh)), 3)

    def test_outgoing_is_flat_with_no_standing_charge(self):
        outgoing = self.tariffs["outgoing"]
        self.assertEqual(set(outgoing.slot_rates_p_kwh), {12.0})
        self.assertEqual(outgoing.standing_charge_p_day, 0.0)

    def test_agile_is_averaged_over_a_week_and_records_the_observed_range(self):
        agile = self.tariffs["agile"]
        raw = [r["value_inc_vat"] for r in self.stub.responses[rate_key(self.stub, "AGILE-24-10-01")]["results"]]
        self.assertEqual(len(raw), 336)
        self.assertEqual(agile.observed_min_p_kwh, min(raw))
        self.assertEqual(agile.observed_max_p_kwh, max(raw))
        self.assertEqual(len(agile.slot_rates_p_kwh), 48)
        self.assertTrue(all(min(raw) <= r <= max(raw) for r in agile.slot_rates_p_kwh))
        self.assertAlmostEqual(sum(agile.slot_rates_p_kwh) / 48, sum(raw) / len(raw), places=6)

    def test_market_is_stamped_with_the_fetch_time(self):
        self.assertEqual(self.market.fetched_at, NOW.isoformat())

    def test_every_recorded_response_is_used(self):
        """A stale entry means core stopped requesting something, so the fixture needs re-recording."""
        stub = Playback.load()
        for region in api_fixtures.RECORD_REGIONS:
            fetch(region, stub)
        with mock.patch.object(core, "_get_json", stub):
            core.lookup_region(api_fixtures.RECORD_POSTCODE)
        self.assertEqual(set(stub.calls), set(stub.responses))


class RequestShapeTests(unittest.TestCase):
    def setUp(self):
        _, self.stub = fetch("C")

    def rate_query(self, product):
        return query(rate_key(self.stub, product))

    def test_agile_asks_for_the_last_seven_days(self):
        q = self.rate_query("AGILE-24-10-01")
        self.assertEqual(q["period_to"], "2026-09-21T14:00Z")
        self.assertEqual(q["period_from"], "2026-09-14T14:00Z")
        self.assertEqual(q["page_size"], "1500")

    def test_time_of_use_tariffs_ask_for_the_next_24_hours(self):
        for product in ("GO-VAR-22-10-14", "COSY-22-12-08", "VAR-22-11-01", "OUTGOING-VAR-24-10-26"):
            with self.subTest(product):
                q = self.rate_query(product)
                self.assertEqual(q["period_from"], "2026-09-21T14:00Z")
                self.assertEqual(q["period_to"], "2026-09-22T14:00Z")

    def test_unaligned_now_is_floored_to_the_half_hour(self):
        # 14:17 must floor to the recorded 14:00 windows; playback raises if any window differs.
        stub = Playback.load()
        with mock.patch.object(core, "_get_json", stub):
            core.fetch_market("C", now=NOW + timedelta(minutes=17))
        with mock.patch.object(core, "_get_json", stub), self.assertRaises(AssertionError):
            core.fetch_market("C", now=NOW + timedelta(minutes=47))  # floors to 14:30, which was not recorded


class ProductFilteringTests(unittest.TestCase):
    def market_with_extra_products(self, *extra):
        stub = Playback.load()
        stub.responses[PRODUCTS_KEY]["results"].extend(extra)
        return fetch("C", stub)

    def product(self, code, direction="IMPORT", **overrides):
        return {"code": code, "full_name": code, "direction": direction, "is_variable": True, "is_prepay": False,
                "available_from": "2025-01-01T00:00:00Z", "available_to": None, **overrides}

    def test_expired_prepay_and_not_yet_available_products_are_ignored(self):
        extras = [
            self.product("GO-VAR-99-01-01", available_from="2026-06-01T00:00:00Z", available_to="2026-09-01T00:00:00Z"),  # expired
            self.product("VAR-99-PREPAY", is_prepay=True, available_from="2026-09-01T00:00:00Z"),
            self.product("AGILE-99-FUTURE", available_from="2027-01-01T00:00:00Z"),
        ]
        market, stub = self.market_with_extra_products(*extras)
        codes = {t.product_code for t in market.import_tariffs}
        self.assertFalse(codes & {"GO-VAR-99-01-01", "VAR-99-PREPAY", "AGILE-99-FUTURE"})
        self.assertFalse([c for c in stub.calls if "-99-" in c], "ignored products must not be fetched")

    def test_newest_available_product_in_a_family_wins(self):
        stub = Playback.load()
        newer = self.product("GO-VAR-26-09-20", available_from="2026-09-20T00:00:00Z")
        stub.responses[PRODUCTS_KEY]["results"].append(newer)
        detail = copy.deepcopy(stub.responses[f"{API}/products/GO-VAR-22-10-14/"])
        detail["code"] = "GO-VAR-26-09-20"
        stub.responses[f"{API}/products/GO-VAR-26-09-20/"] = detail
        for key in [k for k in stub.responses if "/products/GO-VAR-22-10-14/electricity-tariffs/" in k]:
            stub.responses[key.replace("/products/GO-VAR-22-10-14/", "/products/GO-VAR-26-09-20/")] = stub.responses[key]
        market, _ = fetch("C", stub)
        self.assertEqual(by_family(market)["go"].product_code, "GO-VAR-26-09-20")

    def test_product_without_the_requested_region_is_reported_unavailable(self):
        stub = Playback.load()
        del stub.responses[f"{API}/products/GO-VAR-22-10-14/"]["single_register_electricity_tariffs"]["_C"]
        market, _ = fetch("C", stub)
        self.assertNotIn("go", by_family(market))
        self.assertEqual(market.unavailable, ["Octopus Go"])
        self.assertIn("variable", by_family(market))

    def test_no_import_products_at_all_is_an_api_error(self):
        stub = Playback.load()
        stub.responses[PRODUCTS_KEY]["results"] = []
        with self.assertRaises(core.OctopusApiError):
            fetch("C", stub)

    def test_unrecorded_request_fails_loudly(self):
        stub = Playback.load()
        with self.assertRaises(AssertionError) as ctx:
            stub("https://api.octopus.energy/v1/products/NOT-RECORDED/")
        self.assertIn("not recorded", str(ctx.exception))


class EndToEndTests(unittest.TestCase):
    def recommend(self, profile):
        stub = Playback.load()
        with mock.patch.object(core, "_get_json", stub):
            return core.recommend_tariff(profile, fetcher=lambda region: core.fetch_market(region, now=NOW))

    def test_overnight_ev_household_on_recorded_rates(self):
        result = self.recommend({"region": "C", "average_usage_kwh": 4200, "has_solar": False, "has_ev": True, "ev_charging_pattern": "overnight"})
        self.assertEqual(result["recommendations"][0]["family"], "go")
        self.assertEqual(result["data_source"]["fetched_at"], NOW.isoformat())
        self.assertEqual(result["data_source"]["unavailable_products"], [])
        json.dumps(result)

    def test_solar_household_gets_import_and_export_from_recorded_rates(self):
        result = self.recommend({"region": "N", "average_usage_kwh": 3500, "has_solar": True, "export_capacity_kw": 3.5, "has_ev": False})
        self.assertEqual([r["role"] for r in result["recommendations"]], ["import", "export"])
        self.assertTrue(result["recommendations"][1]["tariff_code"].endswith("-N"))

    def test_every_archetype_runs_on_recorded_rates(self):
        import test_core
        for name, extra in test_core.ARCHETYPE_PROFILES.items():
            with self.subTest(name):
                result = self.recommend({**test_core.BASE, **extra})
                self.assertEqual(result["archetype"], name)


class LookupRegionTests(unittest.TestCase):
    KEY = f"{API}/industry/grid-supply-points/?postcode=SW1A1AA"

    def lookup(self, postcode, stub=None):
        stub = stub or Playback.load()
        with mock.patch.object(core, "_get_json", stub):
            return core.lookup_region(postcode), stub

    def test_recorded_postcode_resolves_to_a_single_region(self):
        result, stub = self.lookup("SW1A 1AA")
        self.assertEqual(result, {
            "postcode": "SW1A 1AA", "ambiguous": False, "region": "C", "region_name": "London",
            "candidates": [{"region": "C", "region_name": "London"}],
        })
        self.assertEqual(stub.calls, [self.KEY])

    def test_postcode_is_normalised_before_the_request(self):
        for variant in ("sw1a 1aa", "SW1A1AA", "  sw1a   1aa  ", "Sw1a\t1Aa"):
            with self.subTest(variant):
                result, stub = self.lookup(variant)
                self.assertEqual(stub.calls, [self.KEY])  # compact and upper-cased; the recorded key
                self.assertEqual(result["postcode"], "SW1A 1AA")

    def test_postcode_spanning_regions_is_flagged_ambiguous_not_guessed(self):
        stub = Playback.load()
        stub.responses[self.KEY]["results"] = [{"group_id": "_C"}, {"group_id": "_A"}, {"group_id": "_C"}]
        result, _ = self.lookup("SW1A 1AA", stub)
        self.assertTrue(result["ambiguous"])
        self.assertIsNone(result["region"])
        self.assertIsNone(result["region_name"])
        self.assertEqual([c["region"] for c in result["candidates"]], ["A", "C"])

    def test_unknown_postcode_is_a_profile_error(self):
        stub = Playback.load()
        stub.responses[self.KEY]["results"] = []
        with self.assertRaises(core.ProfileError) as ctx:
            self.lookup("SW1A 1AA", stub)
        self.assertIn("No electricity supply region", str(ctx.exception))

    def test_unrecognised_group_ids_are_ignored(self):
        stub = Playback.load()
        stub.responses[self.KEY]["results"] = [{"group_id": "_Z"}, {"group_id": ""}, {}]
        with self.assertRaises(core.ProfileError):
            self.lookup("SW1A 1AA", stub)

    def test_malformed_postcodes_are_rejected_without_any_request(self):
        for bad in ("", "hello", "SW1A", "12345", "SW1A 1A", "SW1A 1AAA", "SW1A-1AA"):
            with self.subTest(bad):
                stub = Playback.load()
                with self.assertRaises(core.ProfileError):
                    self.lookup(bad, stub)
                self.assertEqual(stub.calls, [], "a malformed postcode must never be sent to the API")

    def test_api_failure_propagates_as_api_error(self):
        with mock.patch.object(core, "_get_json", side_effect=core.OctopusApiError("down")):
            with self.assertRaises(core.OctopusApiError):
                core.lookup_region("SW1A 1AA")


class PaginationTests(unittest.TestCase):
    def test_paginate_follows_next_until_exhausted(self):
        pages = {
            "https://x/a": {"results": [1, 2], "next": "https://x/b"},
            "https://x/b": {"results": [3], "next": "https://x/c"},
            "https://x/c": {"results": [4, 5], "next": None},
        }
        stub = mock.Mock(side_effect=lambda url, params=None: pages[url])
        with mock.patch.object(core, "_get_json", stub):
            self.assertEqual(core._paginate("https://x/a", {"page_size": 2}), [1, 2, 3, 4, 5])
        self.assertEqual([c.args[0] for c in stub.call_args_list], ["https://x/a", "https://x/b", "https://x/c"])
        self.assertEqual(stub.call_args_list[0].args[1], {"page_size": 2})
        self.assertEqual(len(stub.call_args_list[1].args), 1)  # the next URL is complete already, so no params are re-sent

    def test_single_page_and_empty_results(self):
        with mock.patch.object(core, "_get_json", return_value={"results": [], "next": None}):
            self.assertEqual(core._paginate("https://x/a"), [])


class GetJsonTests(unittest.TestCase):
    """core._get_json itself, with urllib patched (no network)."""

    def setUp(self):
        patcher = mock.patch.object(core.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def urlopen(self, *effects):
        return mock.patch.object(core.urllib.request, "urlopen", side_effect=list(effects))

    def ok(self, payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_success_parses_json_and_sends_params_and_user_agent(self):
        with self.urlopen(self.ok({"a": 1})) as opener:
            self.assertEqual(core._get_json("https://x/y", {"b": 2, "c": "d e"}), {"a": 1})
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://x/y?b=2&c=d+e")
        self.assertIn("octopus-tariff-advisor", request.get_header("User-agent"))

    def test_client_error_is_not_retried(self):
        error = urllib.error.HTTPError("https://x/y", 404, "Not Found", None, io.BytesIO(b""))
        with self.urlopen(error, self.ok({})) as opener:
            with self.assertRaises(core.OctopusApiError) as ctx:
                core._get_json("https://x/y")
        self.assertEqual(opener.call_count, 1)
        self.assertIn("404", str(ctx.exception))

    def test_server_error_is_retried_then_succeeds(self):
        error = urllib.error.HTTPError("https://x/y", 503, "Unavailable", None, io.BytesIO(b""))
        with self.urlopen(error, self.ok({"ok": True})) as opener:
            self.assertEqual(core._get_json("https://x/y"), {"ok": True})
        self.assertEqual(opener.call_count, 2)
        self.sleep.assert_called_once()

    def test_persistent_network_failure_gives_up_after_configured_retries(self):
        with self.urlopen(*[urllib.error.URLError("down")] * (core.HTTP_RETRIES + 1)) as opener:
            with self.assertRaises(core.OctopusApiError) as ctx:
                core._get_json("https://x/y")
        self.assertEqual(opener.call_count, core.HTTP_RETRIES + 1)
        self.assertIn("Could not reach the Octopus API", str(ctx.exception))

    def test_invalid_json_is_retried_then_reported(self):
        with self.urlopen(*[io.BytesIO(b"<html>oops</html>") for _ in range(core.HTTP_RETRIES + 1)]) as opener:
            with self.assertRaises(core.OctopusApiError):
                core._get_json("https://x/y")
        self.assertEqual(opener.call_count, core.HTTP_RETRIES + 1)


if __name__ == "__main__":
    unittest.main()
