"""Tests for mcp_server.py: the MCP adapter over core.py. No network.

Needs the `mcp` package (pip install -r requirements-mcp.txt, Python 3.10+). Without it
the MCP tests are skipped so test_core and test_api still run on the standard library.

Tools are called through an in-memory mcp Client, with core._get_json served by the
recorded API responses (api_fixtures.Playback). Run from this folder:

    python -m unittest -v test_mcp_server
"""

import ast
import asyncio
import json
import sys
import threading
import time
import typing
import unittest
from pathlib import Path
from unittest import mock

import api_fixtures
import core
from api_fixtures import NOW, Playback

try:
    import mcp_server
    from mcp import StdioServerParameters
    from mcp.client import Client
except ModuleNotFoundError as exc:  # only "mcp" (or its dependencies) may be missing
    if not (exc.name or "").split(".")[0] in {"mcp", "pydantic", "anyio", "httpx2", "starlette", "uvicorn"}:
        raise
    mcp_server = None

needs_mcp = unittest.skipIf(mcp_server is None, "mcp is not installed (pip install -r requirements-mcp.txt)")

HERE = Path(__file__).parent
PROFILE = {"region": "C", "average_usage_kwh": 4200, "has_solar": False, "has_ev": True, "ev_charging_pattern": "overnight"}


def fixed_time_fetch(region):
    """core.fetch_market pinned to the time the fixtures were recorded at."""
    return core.fetch_market(region, now=NOW)


def text_of(result):
    return " ".join(block.text for block in result.content if hasattr(block, "text"))


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@needs_mcp
class McpTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs tools in-memory with recorded API responses and a fresh cache per test."""

    def setUp(self):
        self.stub = Playback.load()
        for patcher in (
            mock.patch.object(core, "_get_json", self.stub),
            mock.patch.object(mcp_server, "market_cache", mcp_server.MarketCache(fetch=fixed_time_fetch)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, name, arguments=None):
        async with Client(mcp_server.mcp) as client:
            return await client.call_tool(name, arguments or {})


@needs_mcp
class ToolListingTests(McpTestCase):
    async def tools(self):
        async with Client(mcp_server.mcp) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    async def test_exposes_exactly_the_three_tools(self):
        self.assertEqual(set(await self.tools()), {"recommend_tariff", "find_region", "list_regions"})

    async def test_recommend_tariff_schema_requires_the_core_fields(self):
        schema = (await self.tools())["recommend_tariff"].input_schema
        self.assertEqual(set(schema["required"]), {"region", "average_usage_kwh", "has_solar", "has_ev"})
        self.assertEqual(schema["properties"]["region"]["enum"], sorted(core.REGIONS))
        pattern = schema["properties"]["ev_charging_pattern"]
        self.assertIn(["overnight", "daytime", "flexible"], [o.get("enum") for o in pattern["anyOf"]])

    async def test_every_tool_is_read_only_and_non_destructive(self):
        for name, tool in (await self.tools()).items():
            with self.subTest(name):
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertTrue(tool.annotations.idempotent_hint)
                self.assertFalse(tool.annotations.destructive_hint)

    async def test_only_list_regions_is_closed_world(self):
        tools = await self.tools()
        self.assertFalse(tools["list_regions"].annotations.open_world_hint)
        self.assertTrue(tools["recommend_tariff"].annotations.open_world_hint)
        self.assertTrue(tools["find_region"].annotations.open_world_hint)

    def test_server_instructions_carry_the_disclaimer(self):
        text = mcp_server.mcp.instructions.lower()
        self.assertIn("not financial advice", text)
        self.assertIn("regulated switching advice", text)

    def test_region_letter_enum_matches_the_core(self):
        self.assertEqual(set(typing.get_args(mcp_server.RegionLetter)), set(core.REGIONS))

    def test_ev_pattern_enum_is_accepted_by_the_core(self):
        for pattern in typing.get_args(mcp_server.EvPattern):
            with self.subTest(pattern):
                core.validate_profile({**PROFILE, "ev_charging_pattern": pattern})


@needs_mcp
class RecommendTariffTests(McpTestCase):
    def expected(self, profile):
        with mock.patch.object(core, "_get_json", Playback.load()):
            return core.recommend_tariff(profile, fetcher=fixed_time_fetch)

    async def test_result_equals_calling_the_core_directly(self):
        result = await self.call("recommend_tariff", PROFILE)
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, self.expected(PROFILE))
        self.assertEqual(json.loads(text_of(result)), result.structured_content)

    async def test_result_carries_recommendations_and_the_disclaimer(self):
        data = (await self.call("recommend_tariff", PROFILE)).structured_content
        self.assertEqual(data["recommendations"][0]["family"], "go")
        self.assertIn("not financial advice", data["disclaimer"].lower())
        self.assertTrue(data["assumptions"])

    async def test_solar_household_gets_import_and_export(self):
        data = (await self.call("recommend_tariff", {"region": "N", "average_usage_kwh": 3500, "has_solar": True, "has_ev": False, "export_capacity_kw": 3.5})).structured_content
        self.assertEqual([r["role"] for r in data["recommendations"]], ["import", "export"])

    async def test_omitted_optional_fields_stay_omitted_so_the_core_can_warn(self):
        base = {"region": "C", "average_usage_kwh": 4000, "has_solar": False, "has_ev": False, "has_battery": True}
        omitted = (await self.call("recommend_tariff", base)).structured_content
        self.assertTrue(any("battery_can_shift_to_offpeak not given" in w for w in omitted["warnings"]))
        explicit = (await self.call("recommend_tariff", {**base, "battery_can_shift_to_offpeak": False})).structured_content
        self.assertFalse(any("battery_can_shift_to_offpeak not given" in w for w in explicit["warnings"]))

    async def test_core_validation_error_keeps_its_message_and_makes_no_request(self):
        result = await self.call("recommend_tariff", {**PROFILE, "ev_charging_pattern": None})
        self.assertTrue(result.is_error)
        self.assertIn("ev_charging_pattern", text_of(result))
        self.assertEqual(self.stub.calls, [], "invalid input must fail before any API request")

    async def test_schema_violations_are_errors_and_make_no_request(self):
        for label, args in {
            "unknown region": {**PROFILE, "region": "Z"},
            "zero usage": {**PROFILE, "average_usage_kwh": 0},
            "negative usage": {**PROFILE, "average_usage_kwh": -5},
            "missing has_ev": {k: v for k, v in PROFILE.items() if k != "has_ev"},
            "bad pattern": {**PROFILE, "ev_charging_pattern": "sometimes"},
        }.items():
            with self.subTest(label):
                result = await self.call("recommend_tariff", args)
                self.assertTrue(result.is_error)
        self.assertEqual(self.stub.calls, [])

    async def test_api_outage_is_a_friendly_tool_error_and_is_not_cached(self):
        attempts = []

        def flaky(region):
            attempts.append(region)
            if len(attempts) == 1:
                raise core.OctopusApiError("connection refused")
            return fixed_time_fetch(region)

        with mock.patch.object(mcp_server, "market_cache", mcp_server.MarketCache(fetch=flaky)):
            failed = await self.call("recommend_tariff", PROFILE)
            self.assertTrue(failed.is_error)
            self.assertIn("temporarily unavailable", text_of(failed))
            recovered = await self.call("recommend_tariff", PROFILE)
        self.assertFalse(recovered.is_error)
        self.assertEqual(attempts, ["C", "C"])

    async def test_repeat_calls_reuse_the_cached_market(self):
        await self.call("recommend_tariff", PROFILE)
        first = len(self.stub.calls)
        await self.call("recommend_tariff", {**PROFILE, "average_usage_kwh": 5000})
        self.assertGreater(first, 0)
        self.assertEqual(len(self.stub.calls), first, "second call for the same region must not hit the API")

    async def test_tool_runs_off_the_event_loop_thread(self):
        seen = []

        def spy(region):
            seen.append(threading.current_thread() is threading.main_thread())
            return fixed_time_fetch(region)

        with mock.patch.object(mcp_server, "market_cache", mcp_server.MarketCache(fetch=spy)):
            await self.call("recommend_tariff", PROFILE)
        self.assertEqual(seen, [False])


@needs_mcp
class FindRegionAndListRegionsTests(McpTestCase):
    async def test_find_region_resolves_the_recorded_postcode(self):
        result = await self.call("find_region", {"postcode": "sw1a 1aa"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["region"], "C")
        self.assertEqual(result.structured_content["region_name"], "London")
        self.assertFalse(result.structured_content["ambiguous"])

    async def test_ambiguous_postcode_is_reported_not_guessed(self):
        self.stub.responses[LookupKey]["results"] = [{"group_id": "_A"}, {"group_id": "_B"}]
        data = (await self.call("find_region", {"postcode": "SW1A 1AA"})).structured_content
        self.assertTrue(data["ambiguous"])
        self.assertIsNone(data["region"])
        self.assertEqual([c["region"] for c in data["candidates"]], ["A", "B"])

    async def test_unknown_postcode_is_an_error_with_a_message(self):
        self.stub.responses[LookupKey]["results"] = []
        result = await self.call("find_region", {"postcode": "SW1A 1AA"})
        self.assertTrue(result.is_error)
        self.assertIn("No electricity supply region", text_of(result))

    async def test_malformed_postcodes_are_errors_and_make_no_request(self):
        for bad in ("hello", "SW1A", "ab", "x" * 30):
            with self.subTest(bad):
                self.assertTrue((await self.call("find_region", {"postcode": bad})).is_error)
        self.assertEqual(self.stub.calls, [])

    async def test_list_regions_returns_all_fourteen_and_needs_no_network(self):
        result = await self.call("list_regions")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, core.REGIONS)
        self.assertEqual(len(result.structured_content), 14)
        self.assertEqual(self.stub.calls, [])


LookupKey = "https://api.octopus.energy/v1/industry/grid-supply-points/?postcode=SW1A1AA"


@needs_mcp
class MarketCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fetched = []

        def fetch(region):
            self.fetched.append(region)
            return core.Market(region, "t", [], [])

        self.cache = mcp_server.MarketCache(fetch=fetch, ttl_s=600, clock=self.clock)

    def test_second_call_within_ttl_is_served_from_cache(self):
        first = self.cache("C")
        self.assertIs(self.cache("C"), first)
        self.assertEqual(self.fetched, ["C"])

    def test_each_region_is_cached_separately(self):
        self.cache("C")
        self.cache("N")
        self.cache("C")
        self.assertEqual(self.fetched, ["C", "N"])

    def test_entry_expires_at_the_ttl(self):
        self.cache("C")
        self.clock.now += 599.9
        self.cache("C")
        self.assertEqual(self.fetched, ["C"])
        self.clock.now += 0.2
        self.cache("C")
        self.assertEqual(self.fetched, ["C", "C"])

    def test_failed_fetch_is_not_cached(self):
        calls = []

        def flaky(region):
            calls.append(region)
            if len(calls) == 1:
                raise core.OctopusApiError("down")
            return core.Market(region, "t", [], [])

        cache = mcp_server.MarketCache(fetch=flaky, clock=self.clock)
        with self.assertRaises(core.OctopusApiError):
            cache("C")
        cache("C")
        self.assertEqual(calls, ["C", "C"])

    def test_concurrent_callers_share_one_fetch(self):
        count = []

        def slow(region):
            count.append(region)
            time.sleep(0.05)
            return core.Market(region, "t", [], [])

        cache = mcp_server.MarketCache(fetch=slow, clock=self.clock)
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(cache("C"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(count), 1)
        self.assertEqual(len({id(r) for r in results}), 1)


@needs_mcp
class StdioSmokeTest(unittest.IsolatedAsyncioTestCase):
    """Launch the real server as a subprocess and talk to it over stdio (offline tool only)."""

    async def test_real_stdio_handshake_and_tool_call(self):
        params = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_server.py")], cwd=str(HERE))

        async def scenario():
            async with Client(params) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                result = await client.call_tool("list_regions", {})
                return names, result

        names, result = await asyncio.wait_for(scenario(), timeout=60)
        self.assertEqual(names, {"recommend_tariff", "find_region", "list_regions"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, core.REGIONS)  # a clean parse proves nothing polluted stdout


class ArchitectureTests(unittest.TestCase):
    """The core must not know about MCP, and the adapter must never write to stdout."""

    def parse(self, name):
        return ast.parse((HERE / name).read_text(encoding="utf-8"))

    def test_core_and_cli_do_not_import_mcp_or_pydantic(self):
        for name in ("core.py", "cli.py"):
            imported = set()
            for node in ast.walk(self.parse(name)):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & {"mcp", "mcp_server", "pydantic", "anyio"}, f"{name}: {imported}")

    def test_mcp_server_never_prints_or_reads_input(self):
        called = {n.func.id for n in ast.walk(self.parse("mcp_server.py")) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(called & {"print", "input"}, called)

    def test_mcp_server_contains_no_tariff_logic(self):
        """The adapter should only delegate: it must not reach into the core's engine."""
        source = (HERE / "mcp_server.py").read_text(encoding="utf-8")
        for engine_name in ("_cost_for_tariff", "classify_profile", "build_recommendations", "_dispatch_day", "ASSUMPTIONS"):
            self.assertNotIn(engine_name, source)


if __name__ == "__main__":
    unittest.main()
