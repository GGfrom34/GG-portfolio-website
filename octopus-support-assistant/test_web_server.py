"""Tests for web_server.py: the web chat adapter over core.py. No network, no
real Anthropic calls, no real Jira calls.

Needs fastapi/uvicorn/anthropic/httpx (pip install -r requirements-web.txt).
Without them the web tests are skipped so test_core/test_help_content still
run on their own.

Two tiers: ChatEndpointTests fakes core.handle_message entirely (proving
web_server.py wires guardrails/SSE/session-store correctly); the guardrail
classes get their own near-verbatim tests since they're duplicated, not
imported, from tariff-advisor/web_server.py. EndToEndIntegrationTests drives
the *real* core.handle_message (a FakeAnthropicClient plus recorded help-page
HTML) through web_server.chat(), proving the wiring (on_usage accumulation,
index_fetch, http_get) actually works together, not just that web_server
calls a stub correctly.

Run from this folder:

    .venv/Scripts/python.exe -m unittest -v test_web_server
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

import core
from fixtures.help_fixtures import Playback
from test_core import (
    FakeAnthropicClient, FakeUsage, answer_response, classify_response,
    insufficient_response, redact_response,
)

try:
    import web_server
except ModuleNotFoundError as exc:  # only fastapi/anthropic/httpx (or their deps) may be missing
    if (exc.name or "").split(".")[0] not in {"fastapi", "anthropic", "httpx", "starlette", "pydantic", "uvicorn"}:
        raise
    web_server = None

needs_web = unittest.skipIf(web_server is None, "fastapi/anthropic not installed (pip install -r requirements-web.txt)")

HERE = Path(__file__).parent

METER_ARTICLE_URL = "https://octopus.energy/help-and-faqs/articles/how-do-i-read-my-meter/"


class FakeRequest:
    """Stands in for starlette.Request: web_server.chat only reads request.client.host."""

    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, ip="1.2.3.4"):
        self.client = self._Client(ip)


class FakeHandleMessage:
    """Stands in for core.handle_message. Records every kwarg it was called
    with (so tests can assert web_server wired client/http_get/index_fetch/
    on_usage through correctly) and drives the given on_usage callback with a
    fixed usage total before returning the canned result."""

    def __init__(self, result: dict, *, usage: tuple[int, int] = (100, 50), raise_exc: Exception = None):
        self.result = result
        self.usage = usage
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def __call__(self, message, history=(), **kwargs):
        self.calls.append({"message": message, "history": list(history), **kwargs})
        if self.raise_exc:
            raise self.raise_exc
        on_usage = kwargs.get("on_usage")
        if on_usage:
            on_usage("classify_message", *self.usage)
        return self.result


def _http_get_from_playback_with_empty_categories(playback: Playback):
    """search_help_pages scans every one of core.CATEGORY_SLUGS by default, and
    the real 'meters' category page (the only one recorded, kept small per
    help_fixtures.py's own docstring) links to several similarly-titled real
    articles besides the one actually recorded -- the real crude token-overlap
    scorer can plausibly pick more than one as a top candidate. Falls back to
    an empty category page, or thin generic filler for an unrecorded article,
    for anything Playback doesn't have; delegates to the real recorded HTML
    (the one article these tests' assertions actually depend on) otherwise."""
    category_urls = {core.category_url(slug) for slug in core.CATEGORY_SLUGS}

    def _get(url: str) -> str:
        if url in playback.responses:
            return playback(url)
        if url in category_urls:
            return "<html><body></body></html>"
        return "<html><body><h1>Unrelated topic</h1><p>Not directly relevant to the query.</p></body></html>"

    return _get


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
class ChatEndpointTests(unittest.TestCase):
    """Drives web_server.chat() directly (bypassing the ASGI/CORS layer) with a faked core.handle_message."""

    def setUp(self):
        for patcher in (
            mock.patch.object(web_server, "sessions", type(web_server.sessions)()),
            mock.patch.object(web_server, "cancelled_sessions", type(web_server.cancelled_sessions)()),
            # A fresh limiter per test: the module-level one is a singleton, and several tests in
            # this class call chat() with the same default IP, so a shared limiter would let earlier
            # tests exhaust later ones' quota.
            mock.patch.object(web_server, "rate_limiter", web_server.RateLimiter(web_server.PER_IP_RATE_LIMIT_PER_MIN)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.budget_path = Path(tmpdir.name) / "budget.json"

    def set_handle_message(self, result: dict, **kwargs) -> FakeHandleMessage:
        fake = FakeHandleMessage(result, **kwargs)
        patcher = mock.patch.object(core, "handle_message", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def set_budget(self, global_limit=1_000_000, per_ip_limit=1_000_000):
        budget = web_server.TokenBudget(self.budget_path, global_limit, per_ip_limit)
        patcher = mock.patch.object(web_server, "token_budget", budget)
        patcher.start()
        self.addCleanup(patcher.stop)
        return budget

    def test_grounded_answer_streams_status_then_text_with_citations_then_done(self):
        fake = self.set_handle_message({"type": "GROUNDED_ANSWER", "text": "Try pressing a button.", "citations": [METER_ARTICLE_URL]})
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="why is my meter blank?"), FakeRequest()))

        self.assertEqual(status, 200)
        self.assertEqual(events[0], ("status", "Thinking…"))
        text_event = next(data for event, data in events if event == "text")
        self.assertIn("Try pressing a button.", text_event)
        self.assertIn(f"Source: {METER_ARTICLE_URL}", text_event)
        self.assertEqual(events[-1][0], "done")
        self.assertIn("session_id", events[-1][1])
        self.assertEqual(len(fake.calls), 1)

    def test_redirect_streams_its_text_as_is(self):
        self.set_handle_message({"type": "REDIRECT", "reason": "account_specific", "text": core.REDIRECT_TEXT})
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="what's my balance?"), FakeRequest()))

        self.assertEqual(status, 200)
        text_event = next(data for event, data in events if event == "text")
        self.assertEqual(text_event, core.REDIRECT_TEXT)

    def test_client_http_get_and_index_fetch_are_wired_through(self):
        fake = self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT})
        self.set_budget()

        run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))

        call = fake.calls[0]
        self.assertIs(call["client"], web_server.anthropic_client)
        self.assertIs(call["http_get"], web_server.http_get)
        self.assertIs(call["index_fetch"], web_server.category_cache)

    def test_on_usage_total_reaches_the_token_budget(self):
        self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT}, usage=(120, 40))
        budget = self.set_budget()

        run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest(ip="9.9.9.9")))

        global_remaining, ip_remaining = budget.remaining("9.9.9.9")
        self.assertEqual(1_000_000 - global_remaining, 160)
        self.assertEqual(1_000_000 - ip_remaining, 160)

    def test_history_round_trips_as_plain_role_content_dicts_across_two_calls(self):
        self.set_handle_message({"type": "GROUNDED_ANSWER", "text": "first reply", "citations": []})
        self.set_budget()
        events, _ = run_stream(web_server.chat(web_server.ChatRequest(message="first message", session_id="s1"), FakeRequest()))
        session_id = events[-1][1]["session_id"]

        fake2 = self.set_handle_message({"type": "GROUNDED_ANSWER", "text": "second reply", "citations": []})
        run_stream(web_server.chat(web_server.ChatRequest(message="second message", session_id=session_id), FakeRequest()))

        passed_history = fake2.calls[0]["history"]
        self.assertEqual(passed_history, [
            {"role": "user", "content": "first message"},
            {"role": "assistant", "content": "first reply"},
        ])

    def test_a_generic_exception_ends_the_stream_gracefully_not_crashing(self):
        self.set_handle_message({}, raise_exc=RuntimeError("boom"))
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))

        self.assertEqual(status, 200)
        text = next(data for event, data in events if event == "text")
        self.assertIn("something went wrong", text)
        self.assertEqual(events[-1][0], "done")

    def test_anthropic_rate_limit_error_gets_the_budget_exhausted_message(self):
        import anthropic
        import httpx

        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(429, request=request)
        self.set_handle_message({}, raise_exc=anthropic.RateLimitError("rate limited", response=response, body=None))
        self.set_budget()

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))

        self.assertEqual(status, 200)
        text = next(data for event, data in events if event == "text")
        self.assertIn(web_server.BUDGET_EXHAUSTED_MESSAGE, text)
        self.assertNotIn("something went wrong", text)

    def test_empty_message_is_rejected_before_handle_message_is_called(self):
        fake = self.set_handle_message({})
        self.set_budget()
        response = web_server.chat(web_server.ChatRequest(message="   "), FakeRequest())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fake.calls, [])

    def test_overlong_message_is_rejected_before_handle_message_is_called(self):
        fake = self.set_handle_message({})
        self.set_budget()
        response = web_server.chat(web_server.ChatRequest(message="x" * (web_server.MAX_INPUT_CHARS + 1)), FakeRequest())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fake.calls, [])

    def test_rate_limited_ip_is_rejected_before_handle_message_is_called(self):
        fake = self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT})
        self.set_budget()
        for _ in range(web_server.PER_IP_RATE_LIMIT_PER_MIN):
            run_stream(web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest()))
        fake.calls.clear()
        response = web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(fake.calls, [])

    def test_session_turn_limit_is_rejected_before_handle_message_is_called(self):
        # Populates history directly rather than looping real chat() calls, so this test's
        # assertion is about the turn-limit check alone, not entangled with the (lower-limit)
        # rate limiter also being exercised MAX_TURNS_PER_SESSION times on the same IP.
        fake = self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT})
        self.set_budget()
        _, history = web_server.get_session("s1")
        for _ in range(web_server.MAX_TURNS_PER_SESSION):
            history.append({"role": "user", "content": "hi"})
            history.append({"role": "assistant", "content": "..."})
        response = web_server.chat(web_server.ChatRequest(message="hi", session_id="s1"), FakeRequest())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(fake.calls, [])

    def test_exhausted_global_budget_stops_the_conversation_up_front(self):
        fake = self.set_handle_message({})
        self.set_budget(global_limit=0, per_ip_limit=1_000_000)
        response = web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(fake.calls, [])

    def test_exhausted_per_ip_budget_stops_that_ip_but_not_others(self):
        self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT})
        budget = self.set_budget(global_limit=1_000_000, per_ip_limit=1)
        budget.record("1.2.3.4", 1)  # already at that IP's cap
        response = web_server.chat(web_server.ChatRequest(message="hi"), FakeRequest(ip="1.2.3.4"))
        self.assertEqual(response.status_code, 429)
        self.assertTrue(budget.can_afford("9.9.9.9", 1))  # a different IP still has its own share

    def test_cancelled_session_skips_the_call_but_still_ends_with_done(self):
        self.set_handle_message({"type": "REDIRECT", "reason": "dispute", "text": core.REDIRECT_TEXT})
        self.set_budget()
        _, history = web_server.get_session("abandoned")
        web_server.mark_cancelled("abandoned")

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="hi", session_id="abandoned"), FakeRequest()))

        self.assertEqual(status, 200)
        self.assertEqual(events, [("done", {"session_id": "abandoned"})])


@needs_web
class CancellationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(web_server, "cancelled_sessions", type(web_server.cancelled_sessions)())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mark_then_consume_returns_true_once(self):
        web_server.mark_cancelled("s1")
        self.assertTrue(web_server.consume_cancelled("s1"))
        self.assertFalse(web_server.consume_cancelled("s1"))  # already consumed

    def test_unmarked_session_is_not_cancelled(self):
        self.assertFalse(web_server.consume_cancelled("never-marked"))

    def test_cancel_endpoint_marks_the_session(self):
        response = web_server.cancel(web_server.CancelRequest(session_id="s2"))
        self.assertEqual(response, {"status": "ok"})
        self.assertTrue(web_server.consume_cancelled("s2"))


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


@needs_web
class EndToEndIntegrationTests(unittest.TestCase):
    """Drives web_server.chat() with the *real* core.handle_message -- a
    FakeAnthropicClient plus recorded help-page HTML stand in for the only
    two genuinely external dependencies. Proves the wiring (on_usage
    accumulation, index_fetch, http_get) works together end-to-end, not just
    that web_server calls a stub correctly."""

    def setUp(self):
        for patcher in (
            mock.patch.object(web_server, "sessions", type(web_server.sessions)()),
            mock.patch.object(web_server, "cancelled_sessions", type(web_server.cancelled_sessions)()),
            mock.patch.object(web_server, "rate_limiter", web_server.RateLimiter(web_server.PER_IP_RATE_LIMIT_PER_MIN)),
            mock.patch.object(web_server, "category_cache", core.CategoryIndexCache()),
            mock.patch.object(web_server, "http_get", _http_get_from_playback_with_empty_categories(Playback.load())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        budget = web_server.TokenBudget(Path(tmpdir.name) / "budget.json", 1_000_000, 1_000_000)
        patcher = mock.patch.object(web_server, "token_budget", budget)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.budget = budget

    def set_client(self, *responses) -> FakeAnthropicClient:
        client = FakeAnthropicClient(list(responses))
        patcher = mock.patch.object(web_server, "anthropic_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_grounded_answer_end_to_end_records_real_usage(self):
        self.set_client(
            classify_response("general", topic_query="how do I read my meter", topic_category_slugs=["meters"]),
            answer_response("Press a button to wake it up.", [METER_ARTICLE_URL]),
        )

        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="how do I read my meter?"), FakeRequest(ip="5.5.5.5")))

        self.assertEqual(status, 200)
        text = next(data for event, data in events if event == "text")
        self.assertIn("Press a button to wake it up.", text)
        self.assertIn(f"Source: {METER_ARTICLE_URL}", text)
        # Two real model calls (classify + answer) each reported their FakeUsage default (10, 5)
        # via on_usage, so 15 tokens each = 30 total should have reached the real budget.
        global_remaining, ip_remaining = self.budget.remaining("5.5.5.5")
        self.assertEqual(1_000_000 - global_remaining, 30)
        self.assertEqual(1_000_000 - ip_remaining, 30)

    def test_account_specific_redirect_end_to_end(self):
        self.set_client(classify_response("account_specific"))
        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="what's my balance?"), FakeRequest()))
        self.assertEqual(status, 200)
        text = next(data for event, data in events if event == "text")
        self.assertEqual(text, core.REDIRECT_TEXT)

    def test_content_gap_end_to_end_never_logs_without_jira_configured(self):
        # No JIRA_* env vars are set in this test process, so jira_config_from_env() returns
        # None and log_content_gap no-ops -- this just proves the real pipeline still completes
        # cleanly (redact_text's model call included) without a configured Jira.
        self.set_client(
            classify_response("general", topic_query="completely unmatched zyzzyx topic"),
            redact_response([]),
        )
        events, status = run_stream(web_server.chat(web_server.ChatRequest(message="completely unmatched zyzzyx topic"), FakeRequest()))
        self.assertEqual(status, 200)
        text = next(data for event, data in events if event == "text")
        self.assertEqual(text, core.REDIRECT_TEXT)


class ArchitectureTests(unittest.TestCase):
    """core.py and cli.py must stay ignorant of the web interface; web_server.py
    must stay ignorant of core's domain logic (reference it via `core.`, not
    reimplement it), mirroring tariff-advisor/test_web_server.py's checks."""

    def parse(self, name):
        return ast.parse((HERE / name).read_text(encoding="utf-8"))

    def imported_top_level_names(self, name):
        imported = set()
        for node in ast.walk(self.parse(name)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        return imported

    def test_cli_does_not_import_fastapi_or_web_server(self):
        imported = self.imported_top_level_names("cli.py")
        self.assertFalse(imported & {"fastapi", "web_server"}, imported)

    def test_core_does_not_import_fastapi_or_web_server(self):
        # core.py legitimately imports anthropic (unlike tariff-advisor's core) -- only
        # the web-framework/web-adapter imports are forbidden here.
        imported = self.imported_top_level_names("core.py")
        self.assertFalse(imported & {"fastapi", "web_server"}, imported)

    @needs_web
    def test_web_server_contains_no_domain_logic(self):
        source = (HERE / "web_server.py").read_text(encoding="utf-8")
        for engine_name in ("CLASSIFY_SYSTEM_PROMPT", "_HelpTextParser", "_score", "redact_patterns", "ANSWER_SYSTEM_PROMPT"):
            self.assertNotIn(engine_name, source)
        self.assertNotIn('"I\'m an independent, unofficial demo assistant', source)  # REDIRECT_TEXT must be referenced via core., not duplicated


if __name__ == "__main__":
    unittest.main()
