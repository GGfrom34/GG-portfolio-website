"""Tests for web_server.py: the web chat adapter over core.py. No network, no real Anthropic calls.

Needs fastapi/uvicorn/anthropic/httpx (pip install -r requirements-web.txt). Without them the
web tests are skipped so test_core, test_api and test_mcp_server still run on their own.

The Anthropic client is faked (FakeAnthropicClient below): it returns canned tool_use/text
content so the tool-loop plumbing, guardrails and SSE framing are exercised without spending
real tokens. Octopus itself is served by the recorded fixtures, exactly like test_mcp_server.py.

Run from this folder:

    .venv/bin/python -m unittest -v test_web_server
"""

from __future__ import annotations

import ast
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import api_fixtures
import core
from api_fixtures import NOW, Playback

try:
    import web_server
except ModuleNotFoundError as exc:  # only fastapi/anthropic/httpx (or their deps) may be missing
    if not (exc.name or "").split(".")[0] in {"fastapi", "anthropic", "httpx", "starlette", "pydantic", "uvicorn"}:
        raise
    web_server = None

needs_web = unittest.skipIf(web_server is None, "fastapi/anthropic not installed (pip install -r requirements-web.txt)")

HERE = Path(__file__).parent


def fixed_time_fetch(region):
    return core.fetch_market(region, now=NOW)


class FakeRequest:
    """Stands in for starlette.Request: web_server.chat only reads request.client.host."""

    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, ip="1.2.3.4"):
        self.client = self._Client(ip)


class FakeBlock:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self):
        return dict(self.__dict__)


def text_block(text):
    # citations=None mirrors the real anthropic SDK's TextBlock, which carries response-only
    # fields that must not be replayed verbatim into the next request (see serialize_assistant_block).
    return FakeBlock(type="text", text=text, citations=None)


def tool_use_block(id, name, input):
    # caller/toolset_name=None mirror the real SDK's ToolUseBlock, for the same reason.
    return FakeBlock(type="tool_use", id=id, name=name, input=input, caller=None, toolset_name=None)


class FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens, self.output_tokens = input_tokens, output_tokens


class FakeMessage:
    def __init__(self, content, stop_reason, usage=FakeUsage(10, 5)):
        self.content, self.stop_reason, self.usage = content, stop_reason, usage


class FakeStreamContext:
    def __init__(self, text_chunks, final):
        self.text_stream = iter(text_chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._final


class FakeMessagesApi:
    def __init__(self, turns):
        """turns: list of (text_chunks, FakeMessage), consumed one per anthropic call."""
        self._turns = list(turns)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        text_chunks, final = self._turns.pop(0)
        return FakeStreamContext(text_chunks, final)


class FakeAnthropicClient:
    def __init__(self, turns):
        self.messages = FakeMessagesApi(turns)


def run_stream(response) -> tuple[list[tuple[str, dict]], int]:
    """Drains a StreamingResponse's body_iterator into (events, status_code)."""
    async def drain():
        events = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            for block in chunk.strip("\n").split("\n\n"):
                if not block:
                    continue
                lines = block.splitlines()
                event = lines[0].removeprefix("event: ")
                data = json.loads(lines[1].removeprefix("data: "))
                events.append((event, data))
        return events
    return asyncio.run(drain()), response.status_code


@needs_web
class ChatToolLoopTests(unittest.TestCase):
    """Drives web_server.chat() directly (bypassing the ASGI/ CORS layer) with a fake Anthropic client."""

    def setUp(self):
        self.stub = Playback.load()
        for patcher in (
            mock.patch.object(core, "_get_json", self.stub),
            mock.patch.object(web_server, "market_cache", core.MarketCache(fetch=fixed_time_fetch)),
            mock.patch.object(web_server, "sessions", type(web_server.sessions)()),
            # A fresh limiter per test: the module-level one is a singleton, and several tests in
            # this class call chat() with the same default IP, so a shared limiter would let earlier
            # tests exhaust later ones' quota.
            mock.patch.object(web_server, "rate_limiter", web_server.RateLimiter(web_server.PER_IP_RATE_LIMIT_PER_MIN)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # A fresh, generous budget per test so guardrail tests can set their own tight ones.
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.budget_path = Path(tmpdir.name) / "budget.json"

    def set_client(self, *turns):
        client = FakeAnthropicClient(list(turns))
        patcher = mock.patch.object(web_server, "anthropic_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def set_budget(self, global_limit=1_000_000, per_ip_limit=1_000_000):
        budget = web_server.TokenBudget(self.budget_path, global_limit, per_ip_limit)
        patcher = mock.patch.object(web_server, "token_budget", budget)
        patcher.start()
        self.addCleanup(patcher.stop)
        return budget

    def test_text_only_reply_streams_and_ends_with_done(self):
        self.set_client((["Hello, ", "world."], FakeMessage([text_block("Hello, world.")], "end_turn")))
        self.set_budget()
        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))
        self.assertEqual(status, 200)
        text = "".join(data for event, data in events if event == "text")
        self.assertEqual(text, "Hello, world.")
        self.assertEqual(events[-1][0], "done")
        self.assertIn("session_id", events[-1][1])

    def test_tool_call_reaches_core_and_result_feeds_back_to_the_model(self):
        profile_args = {"region": "C", "average_usage_kwh": 4200, "has_solar": False, "has_ev": True, "ev_charging_pattern": "overnight"}
        self.set_client(
            (["Checking…"], FakeMessage([tool_use_block("t1", "recommend_tariff", profile_args)], "tool_use")),
            (["Here's what I found."], FakeMessage([text_block("Here's what I found.")], "end_turn")),
        )
        client = web_server.anthropic_client
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="I have an EV, overnight charging, 4200 kwh, London, no solar"), FakeRequest()))

        self.assertEqual(status, 200)
        statuses = [data for event, data in events if event == "status"]
        self.assertTrue(any("recommend tariff" in s for s in statuses))
        # The second anthropic call's messages must carry a non-error tool_result.
        second_call_messages = client.messages.calls[1]["messages"]
        tool_result_msg = next(m for m in second_call_messages if m["role"] == "user" and isinstance(m["content"], list))
        result_block = tool_result_msg["content"][0]
        self.assertEqual(result_block["type"], "tool_result")
        self.assertFalse(result_block["is_error"])
        result_payload = json.loads(result_block["content"])
        self.assertIn("recommendations", result_payload)

    def test_assistant_history_omits_response_only_fields_the_api_rejects_on_replay(self):
        """Regression test: the real anthropic SDK's TextBlock/ToolUseBlock carry response-only
        fields (citations, caller, toolset_name) that the Messages API's request-side schema does
        not accept back. Blindly replaying block.model_dump() into history broke every follow-up
        turn. Assert the serialized history only ever carries the minimal, request-safe keys."""
        self.set_client(
            (["Checking…"], FakeMessage([tool_use_block("t1", "recommend_tariff", {"region": "C"})], "tool_use")),
            (["All done."], FakeMessage([text_block("All done.")], "end_turn")),
        )
        self.set_budget()

        run_stream(web_server.chat(web_server.ChatRequest(message="hi", session_id="s1"), FakeRequest()))

        _, history = web_server.get_session("s1")
        assistant_messages = [m for m in history if m["role"] == "assistant"]
        self.assertEqual(len(assistant_messages), 2)
        tool_use_content = assistant_messages[0]["content"][0]
        self.assertEqual(set(tool_use_content), {"type", "id", "name", "input"})
        text_content = assistant_messages[1]["content"][0]
        self.assertEqual(set(text_content), {"type", "text"})

    def test_a_client_side_error_before_any_request_ends_the_stream_gracefully(self):
        """Regression test: a missing/misconfigured API key raises a plain TypeError from the SDK's
        header validation, before any HTTP request -- not an anthropic.APIError. The stream must
        still end cleanly with a friendly message and a `done` event, not crash uncaught."""
        class RaisingMessagesApi:
            calls: list = []

            def stream(self, **kwargs):
                raise TypeError("Could not resolve authentication method")

        client = FakeAnthropicClient([])
        client.messages = RaisingMessagesApi()
        patcher = mock.patch.object(web_server, "anthropic_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))

        self.assertEqual(status, 200)
        text = "".join(data for event, data in events if event == "text")
        self.assertIn("something went wrong", text)
        self.assertEqual(events[-1][0], "done")

    def test_profile_error_from_a_bad_region_is_an_error_tool_result(self):
        self.set_client(
            (["Checking…"], FakeMessage([tool_use_block("t1", "recommend_tariff", {"region": "Z"})], "tool_use")),
            (["Sorry about that."], FakeMessage([text_block("Sorry about that.")], "end_turn")),
        )
        client = web_server.anthropic_client
        self.set_budget()

        run_stream(web_server.chat(web_server.ChatRequest(message="region Z please"), FakeRequest()))

        second_call_messages = client.messages.calls[1]["messages"]
        tool_result_msg = next(m for m in second_call_messages if m["role"] == "user" and isinstance(m["content"], list))
        self.assertTrue(tool_result_msg["content"][0]["is_error"])

    def test_empty_message_is_rejected_before_any_model_call(self):
        self.set_client()  # no turns queued: a call would raise IndexError
        self.set_budget()
        response = web_server.chat(web_server.ChatRequest(message="   "), FakeRequest())
        self.assertEqual(response.status_code, 400)

    def test_overlong_message_is_rejected(self):
        self.set_client()
        self.set_budget()
        response = web_server.chat(web_server.ChatRequest(message="x" * (web_server.MAX_INPUT_CHARS + 1)), FakeRequest())
        self.assertEqual(response.status_code, 400)

    def test_exhausted_global_budget_stops_the_conversation_up_front(self):
        self.set_client()
        self.set_budget(global_limit=0, per_ip_limit=1_000_000)
        response = web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest())
        self.assertEqual(response.status_code, 429)

    def test_exhausted_per_ip_budget_stops_that_ip_but_not_others(self):
        self.set_client((["hi"], FakeMessage([text_block("hi")], "end_turn")))
        budget = self.set_budget(global_limit=1_000_000, per_ip_limit=1)
        budget.record("1.2.3.4", 1)  # already at that IP's cap
        response = web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest(ip="1.2.3.4"))
        self.assertEqual(response.status_code, 429)
        # a different IP still has its own share
        self.assertTrue(budget.can_afford("9.9.9.9", 1))


@needs_web
class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit_then_blocks(self):
        limiter = web_server.RateLimiter(limit_per_min=3)
        allowed = [limiter.allow("ip", now=0.0) for _ in range(4)]
        self.assertEqual(allowed, [True, True, True, False])

    def test_separate_keys_have_separate_budgets(self):
        limiter = web_server.RateLimiter(limit_per_min=1)
        self.assertTrue(limiter.allow("a", now=0.0))
        self.assertTrue(limiter.allow("b", now=0.0))
        self.assertFalse(limiter.allow("a", now=0.5))

    def test_old_hits_fall_out_of_the_window(self):
        limiter = web_server.RateLimiter(limit_per_min=1)
        self.assertTrue(limiter.allow("a", now=0.0))
        self.assertFalse(limiter.allow("a", now=30.0))
        self.assertTrue(limiter.allow("a", now=61.0))


@needs_web
class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.path = Path(tmpdir.name) / "budget.json"
        self.clock_value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def clock(self):
        return self.clock_value

    def test_global_cap_blocks_regardless_of_which_key(self):
        budget = web_server.TokenBudget(self.path, global_limit=10, per_key_limit=10, clock=self.clock)
        budget.record("a", 10)
        self.assertFalse(budget.can_afford("b", 1))

    def test_per_key_cap_blocks_only_that_key(self):
        budget = web_server.TokenBudget(self.path, global_limit=1000, per_key_limit=5, clock=self.clock)
        budget.record("a", 5)
        self.assertFalse(budget.can_afford("a", 1))
        self.assertTrue(budget.can_afford("b", 1))

    def test_state_survives_reconstruction_same_day(self):
        first = web_server.TokenBudget(self.path, global_limit=100, per_key_limit=100, clock=self.clock)
        first.record("a", 40)
        second = web_server.TokenBudget(self.path, global_limit=100, per_key_limit=100, clock=self.clock)
        global_remaining, key_remaining = second.remaining("a")
        self.assertEqual(global_remaining, 60)
        self.assertEqual(key_remaining, 60)

    def test_resets_on_a_new_utc_day(self):
        budget = web_server.TokenBudget(self.path, global_limit=100, per_key_limit=100, clock=self.clock)
        budget.record("a", 100)
        self.assertFalse(budget.can_afford("a", 1))
        self.clock_value += timedelta(days=1)
        self.assertTrue(budget.can_afford("a", 1))

    def test_corrupt_state_file_is_treated_as_empty(self):
        self.path.write_text("not json")
        budget = web_server.TokenBudget(self.path, global_limit=10, per_key_limit=10, clock=self.clock)
        self.assertTrue(budget.can_afford("a", 10))


@needs_web
class HealthAndCorsTests(unittest.TestCase):
    def test_health_endpoint(self):
        self.assertEqual(web_server.health(), {"status": "ok"})

    def test_cors_reflects_allowed_origin_via_the_asgi_app(self):
        import importlib
        import os

        from fastapi.testclient import TestClient
        # The middleware reads ALLOWED_ORIGIN from the environment at module-import time, so the
        # env var (not the module attribute) must be set before reloading to rebuild the app.
        with mock.patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://example.com"}):
            reloaded = importlib.reload(web_server)
            client = TestClient(reloaded.app)
            response = client.get("/health", headers={"Origin": "https://example.com"})
            self.assertEqual(response.headers.get("access-control-allow-origin"), "https://example.com")
        importlib.reload(web_server)  # restore module state (default ALLOWED_ORIGIN) for later tests


class ArchitectureTests(unittest.TestCase):
    """core.py and cli.py must stay ignorant of the web interface, mirroring test_mcp_server.py's checks for MCP."""

    def parse(self, name):
        return ast.parse((HERE / name).read_text(encoding="utf-8"))

    def test_core_and_cli_do_not_import_fastapi_or_anthropic(self):
        for name in ("core.py", "cli.py"):
            imported = set()
            for node in ast.walk(self.parse(name)):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & {"fastapi", "anthropic", "web_server"}, f"{name}: {imported}")

    @needs_web
    def test_web_server_contains_no_tariff_logic(self):
        source = (HERE / "web_server.py").read_text(encoding="utf-8")
        for engine_name in ("_cost_for_tariff", "classify_profile", "build_recommendations", "_dispatch_day", "ASSUMPTIONS"):
            self.assertNotIn(engine_name, source)


if __name__ == "__main__":
    unittest.main()
