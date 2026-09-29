"""Offline unit tests for core.py: classification, redaction, Jira logging,
grounded-answer validation, and handle_message's dispatch logic. No network,
no real Anthropic tokens spent, no real Jira calls -- every external call is
faked and injected exactly the way core.py's own DI parameters expect.

See test_help_content.py for the fetch/parse/retrieval layer's tests.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import core

# ---------------------------------------------------------------------------
# Fakes: a minimal stand-in for the Anthropic Messages API, following the
# same shape as tariff-advisor/test_web_server.py's FakeAnthropicClient.
# ---------------------------------------------------------------------------


class FakeBlock:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def text_block(text: str) -> FakeBlock:
    return FakeBlock(type="text", text=text)


def tool_use_block(name: str, input_: dict) -> FakeBlock:
    return FakeBlock(type="tool_use", id="toolu_fake", name=name, input=input_)


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeMessagesApi:
    def __init__(self, responses, on_call=None):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self._on_call = on_call

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._on_call:
            self._on_call(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessagesApi.create called more times than responses were queued")
        return self._responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses=(), on_call=None):
        self.messages = FakeMessagesApi(responses, on_call=on_call)


def classify_response(category: str, *, topic_query: str = "test topic",
                       rationale: str = "test rationale", topic_category_slugs=None) -> FakeMessage:
    return FakeMessage([tool_use_block("classify_message", {
        "category": category, "topic_query": topic_query, "rationale": rationale,
        "topic_category_slugs": topic_category_slugs or [],
    })])


def answer_response(text: str, citations: list[str]) -> FakeMessage:
    return FakeMessage([tool_use_block("respond_from_sources", {"outcome": "answer", "text": text, "citations": citations})])


def insufficient_response() -> FakeMessage:
    return FakeMessage([tool_use_block("respond_from_sources", {"outcome": "insufficient"})])


def redact_response(substrings: list[str]) -> FakeMessage:
    return FakeMessage([tool_use_block("flag_identifying_substrings", {"substrings": substrings})])


def _poison_request(*args, **kwargs):
    raise AssertionError("Jira should not have been called for this classification")


# ---------------------------------------------------------------------------
# Architecture: cli.py stays the only file touching stdin/stdout and never
# talks to Anthropic or the environment directly -- the mirror image of
# tariff-advisor's "core never imports anthropic" check.
# ---------------------------------------------------------------------------

class ArchitectureTests(unittest.TestCase):
    def test_cli_does_not_import_anthropic_or_os(self):
        source = (Path(__file__).parent / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("anthropic", imported)
        self.assertNotIn("os", imported)
        self.assertNotIn("os.environ", source)

    def test_core_has_no_print_or_input(self):
        source = (Path(__file__).parent / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_names = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn("print", called_names)
        self.assertNotIn("input", called_names)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class ClassificationTests(unittest.TestCase):
    def test_all_categories_parsed(self):
        for category in core.CLASSIFICATION_CATEGORIES:
            client = FakeAnthropicClient([classify_response(category)])
            result = core.classify_message("a test message", client=client)
            self.assertEqual(result.category, category)

    def test_missing_tool_use_raises(self):
        client = FakeAnthropicClient([FakeMessage([text_block("no tool call")])])
        with self.assertRaises(core.ClassificationError):
            core.classify_message("test", client=client)

    def test_invalid_category_raises(self):
        client = FakeAnthropicClient([FakeMessage([tool_use_block("classify_message", {"category": "bogus", "rationale": "x"})])])
        with self.assertRaises(core.ClassificationError):
            core.classify_message("test", client=client)

    def test_unknown_topic_category_slugs_are_filtered_out(self):
        client = FakeAnthropicClient([classify_response("general", topic_category_slugs=["meters", "not-a-real-slug"])])
        result = core.classify_message("how do I read my meter", client=client)
        self.assertEqual(result.topic_category_slugs, ["meters"])

    def test_blank_topic_query_falls_back_to_the_message(self):
        client = FakeAnthropicClient([FakeMessage([tool_use_block("classify_message", {"category": "general", "rationale": "x", "topic_query": ""})])])
        result = core.classify_message("how do I read my meter", client=client)
        self.assertEqual(result.topic_query, "how do I read my meter")


# ---------------------------------------------------------------------------
# Redaction -- synthetic PII, documented proof of what's caught and what
# isn't, per the spec.
# ---------------------------------------------------------------------------

class RedactPatternsTests(unittest.TestCase):
    def test_catches_email(self):
        out = core.redact_patterns("contact me at jane.doe@example.com please")
        self.assertIn("[REDACTED-EMAIL]", out)
        self.assertNotIn("jane.doe@example.com", out)

    def test_catches_uk_mobile_phone(self):
        out = core.redact_patterns("call me on 07911 123456 anytime")
        self.assertIn("[REDACTED-PHONE]", out)
        self.assertNotIn("07911 123456", out)

    def test_catches_uk_postcode(self):
        out = core.redact_patterns("I live at SW1A 1AA in London")
        self.assertIn("[REDACTED-POSTCODE]", out)
        self.assertNotIn("SW1A 1AA", out)

    def test_catches_account_style_reference(self):
        out = core.redact_patterns("my account is A-1234ABCD")
        self.assertIn("[REDACTED-REF]", out)
        self.assertNotIn("A-1234ABCD", out)

    def test_catches_long_digit_run_meter_style_number(self):
        out = core.redact_patterns("my MPAN is 1200034567890")
        self.assertIn("[REDACTED-REF]", out)
        self.assertNotIn("1200034567890", out)

    def test_leaves_ordinary_prose_untouched(self):
        text = "How do I read my meter and submit a reading?"
        self.assertEqual(core.redact_patterns(text), text)

    def test_known_false_positive_a_long_kwh_figure(self):
        # Documented trade-off (see core.py's _METER_OR_LONG_NUMBER_RE comment):
        # any 6-13 digit run is treated as a possible reference number, so an
        # ordinary large usage figure with no PII meaning is also redacted.
        # Honestly documented here rather than hidden.
        out = core.redact_patterns("I used 1234567 kWh last year")
        self.assertIn("[REDACTED-REF]", out)


class FindIdentifyingSubstringsTests(unittest.TestCase):
    def test_flags_prose_name_and_address(self):
        client = FakeAnthropicClient([redact_response(["John Smith", "42 Example Street"])])
        result = core.find_identifying_substrings("Hi, I'm John Smith and I live at 42 Example Street.", client=client)
        self.assertEqual(set(result), {"John Smith", "42 Example Street"})

    def test_drops_a_hallucinated_non_verbatim_substring(self):
        client = FakeAnthropicClient([redact_response(["Jane Doe"])])  # not present in the input text
        result = core.find_identifying_substrings("Hi, I'm John Smith.", client=client)
        self.assertEqual(result, [])

    def test_no_tool_use_returns_empty_rather_than_raising(self):
        client = FakeAnthropicClient([FakeMessage([text_block("nothing to flag")])])
        result = core.find_identifying_substrings("hello", client=client)
        self.assertEqual(result, [])


class RedactTextTests(unittest.TestCase):
    def test_pattern_and_freetext_passes_combine(self):
        client = FakeAnthropicClient([redact_response(["John Smith"])])
        text = "I'm John Smith, my email is john@example.com and my account is A-1234ABCD"
        out = core.redact_text(text, client=client)
        self.assertNotIn("John Smith", out)
        self.assertNotIn("john@example.com", out)
        self.assertNotIn("A-1234ABCD", out)

    def test_known_gap_a_bare_nickname_with_no_context_is_not_caught(self):
        # Honest limitation: if neither the regex pass nor the free-text model
        # call (faked here as finding nothing) flags a context-free nickname,
        # it survives redaction. Documented, not hidden.
        client = FakeAnthropicClient([redact_response([])])
        out = core.redact_text("just call me Sparky", client=client)
        self.assertIn("Sparky", out)


# ---------------------------------------------------------------------------
# Jira content-gap logging
# ---------------------------------------------------------------------------

def make_jira_config(**overrides) -> core.JiraConfig:
    defaults = dict(site_url="https://example.atlassian.net", email="bot@example.com",
                     api_token="tok", project_key="SCRUM", sprint_id=4)
    defaults.update(overrides)
    return core.JiraConfig(**defaults)


class FakeJiraRequest:
    """Fakes core._jira_request: routes by (method, path) to canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.create_response = {"key": "SCRUM-999"}
        self.get_response_text = "the stored redacted question"
        self.fail_create = False
        self.fail_get = False

    def __call__(self, config, method, path, *, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "issue":
            if self.fail_create:
                raise core.JiraLoggingError("simulated create failure")
            return self.create_response
        if method == "GET" and path.startswith("issue/"):
            if self.fail_get:
                raise core.JiraLoggingError("simulated get failure")
            return {"fields": {"description": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": self.get_response_text}]}],
            }}}
        raise AssertionError(f"unexpected Jira request: {method} {path}")


class FakeJiraAgileRequest:
    """Fakes core._jira_agile_request for sprint add/verify calls.

    `verify_succeeds_after` simulates real observed behavior: the sprint
    search-index can briefly lag behind an add-to-sprint write, so the first
    N verification GETs report the issue missing before it "catches up"."""

    def __init__(self, *, issue_in_sprint: bool = True, verify_succeeds_after: int = 0):
        self.calls: list[tuple[str, str, object]] = []
        self.issue_in_sprint = issue_in_sprint
        self.verify_succeeds_after = verify_succeeds_after
        self._verify_attempts = 0

    def __call__(self, config, method, path, *, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.startswith("sprint/"):
            return None
        if method == "GET" and path.startswith("sprint/"):
            match = re.search(r"key%3D([A-Z]+-\d+)", path)
            key = match.group(1) if match else None
            self._verify_attempts += 1
            found = self.issue_in_sprint and key and self._verify_attempts > self.verify_succeeds_after
            return {"issues": [{"key": key}]} if found else {"issues": []}
        raise AssertionError(f"unexpected Jira agile request: {method} {path}")


class JiraConfigFromEnvTests(unittest.TestCase):
    def test_none_when_any_of_five_vars_missing(self):
        env = {"JIRA_SITE_URL": "https://x.atlassian.net", "JIRA_EMAIL": "a@b.com",
               "JIRA_API_TOKEN": "t", "JIRA_PROJECT_KEY": "SCRUM"}  # JIRA_SPRINT_ID missing
        self.assertIsNone(core.jira_config_from_env(env))

    def test_populated_when_all_five_vars_present(self):
        env = {"JIRA_SITE_URL": "https://x.atlassian.net/", "JIRA_EMAIL": "a@b.com",
               "JIRA_API_TOKEN": "t", "JIRA_PROJECT_KEY": "SCRUM", "JIRA_SPRINT_ID": "4"}
        config = core.jira_config_from_env(env)
        self.assertEqual(config.site_url, "https://x.atlassian.net")  # trailing slash stripped
        self.assertEqual(config.sprint_id, 4)

    def test_none_when_sprint_id_is_not_numeric(self):
        env = {"JIRA_SITE_URL": "https://x.atlassian.net", "JIRA_EMAIL": "a@b.com",
               "JIRA_API_TOKEN": "t", "JIRA_PROJECT_KEY": "SCRUM", "JIRA_SPRINT_ID": "not-a-number"}
        self.assertIsNone(core.jira_config_from_env(env))


class CreateContentGapIssueTests(unittest.TestCase):
    def test_payload_contains_only_the_redacted_text(self):
        config = make_jira_config()
        request = FakeJiraRequest()
        issue_key = core.create_content_gap_issue(config, "how do I do [REDACTED-REF] thing", request=request)
        self.assertEqual(issue_key, "SCRUM-999")
        method, path, body = request.calls[0]
        self.assertEqual((method, path), ("POST", "issue"))
        self.assertEqual(body["fields"]["project"], {"key": "SCRUM"})
        self.assertEqual(body["fields"]["issuetype"], {"name": "Task"})
        payload_text = body["fields"]["description"]["content"][0]["content"][0]["text"]
        self.assertEqual(payload_text, "how do I do [REDACTED-REF] thing")

    def test_missing_key_in_response_raises(self):
        config = make_jira_config()
        request = FakeJiraRequest()
        request.create_response = {}
        with self.assertRaises(core.JiraLoggingError):
            core.create_content_gap_issue(config, "q", request=request)


class LogContentGapTests(unittest.TestCase):
    def test_returns_none_and_makes_no_calls_when_config_is_none(self):
        request = FakeJiraRequest()
        agile = FakeJiraAgileRequest()
        self.assertIsNone(core.log_content_gap(None, "q", request=request, agile_request=agile))
        self.assertEqual(request.calls, [])
        self.assertEqual(agile.calls, [])

    def test_success_path_creates_files_into_sprint_and_verifies(self):
        config = make_jira_config()
        request, agile = FakeJiraRequest(), FakeJiraAgileRequest(issue_in_sprint=True)
        request.get_response_text = "redacted question"
        result = core.log_content_gap(config, "redacted question", request=request, agile_request=agile)
        self.assertEqual(result, "SCRUM-999")
        self.assertTrue(any(m == "POST" and p == "sprint/4/issue" for m, p, _ in agile.calls))

    def test_swallows_a_create_failure(self):
        config = make_jira_config()
        request = FakeJiraRequest()
        request.fail_create = True
        self.assertIsNone(core.log_content_gap(config, "q", request=request, agile_request=FakeJiraAgileRequest()))

    def test_retries_verification_past_a_transient_search_index_lag(self):
        # Regression test: observed live that add-to-sprint's write can briefly
        # lag behind the sprint search endpoint used to verify it, producing a
        # false "not confirmed" for an issue that really is in the sprint.
        config = make_jira_config()
        request = FakeJiraRequest()
        request.get_response_text = "q"
        agile = FakeJiraAgileRequest(issue_in_sprint=True, verify_succeeds_after=2)
        result = core.log_content_gap(config, "q", request=request, agile_request=agile, sprint_check_delay_s=0)
        self.assertEqual(result, "SCRUM-999")

    def test_returns_none_when_not_confirmed_in_sprint(self):
        config = make_jira_config()
        request = FakeJiraRequest()
        request.get_response_text = "q"
        agile = FakeJiraAgileRequest(issue_in_sprint=False)
        self.assertIsNone(core.log_content_gap(config, "q", request=request, agile_request=agile, sprint_check_delay_s=0))

    def test_returns_none_when_readback_does_not_contain_the_question(self):
        config = make_jira_config()
        request = FakeJiraRequest()
        request.get_response_text = "something else entirely"
        agile = FakeJiraAgileRequest(issue_in_sprint=True)
        self.assertIsNone(core.log_content_gap(config, "q", request=request, agile_request=agile))


# ---------------------------------------------------------------------------
# Grounded-answer generation: fails closed
# ---------------------------------------------------------------------------

def make_corpus() -> core.HelpCorpus:
    return core.HelpCorpus([core.HelpPage(
        url="https://octopus.energy/help-and-faqs/articles/how-do-i-read-my-meter/",
        title="How do I read my meter and submit a meter reading?",
        text="There are a few different kinds of meters...",
        fetched_at="2026-01-01T00:00:00+00:00",
    )])


class GroundedAnswerTests(unittest.TestCase):
    def test_well_formed_answer_is_returned(self):
        corpus = make_corpus()
        client = FakeAnthropicClient([answer_response("Here's how to read your meter...", [corpus.pages[0].url])])
        result = core.generate_grounded_answer("how do I read my meter", [], corpus, client=client)
        self.assertIsInstance(result, core.GroundedAnswer)
        self.assertEqual(result.citations, [corpus.pages[0].url])

    def test_insufficient_outcome_returns_none(self):
        corpus = make_corpus()
        client = FakeAnthropicClient([insufficient_response()])
        self.assertIsNone(core.generate_grounded_answer("unrelated question", [], corpus, client=client))

    def test_citation_outside_the_corpus_is_rejected(self):
        corpus = make_corpus()
        client = FakeAnthropicClient([answer_response("text", ["https://octopus.energy/not-a-fetched-page/"])])
        self.assertIsNone(core.generate_grounded_answer("q", [], corpus, client=client))

    def test_empty_text_is_rejected(self):
        corpus = make_corpus()
        client = FakeAnthropicClient([answer_response("", [corpus.pages[0].url])])
        self.assertIsNone(core.generate_grounded_answer("q", [], corpus, client=client))

    def test_malformed_tool_call_is_rejected(self):
        corpus = make_corpus()
        client = FakeAnthropicClient([FakeMessage([text_block("oops, no tool use")])])
        self.assertIsNone(core.generate_grounded_answer("q", [], corpus, client=client))

    def test_empty_corpus_short_circuits_without_calling_the_model(self):
        client = FakeAnthropicClient([])  # would raise if .create() were called
        result = core.generate_grounded_answer("q", [], core.HelpCorpus([]), client=client)
        self.assertIsNone(result)
        self.assertEqual(client.messages.calls, [])


# ---------------------------------------------------------------------------
# handle_message: end-to-end dispatch, fully faked (client, http_get, Jira)
# ---------------------------------------------------------------------------

METER_ARTICLE_URL = "https://octopus.energy/help-and-faqs/articles/how-do-i-read-my-meter/"
METER_ARTICLE_TITLE = "How do I read my meter and submit a meter reading?"


def _category_html(links: list[tuple[str, str]]) -> str:
    anchors = "".join(f'<a href="{url}"><span>{title}</span></a>' for url, title in links)
    return f"<html><body>{anchors}</body></html>"


def _article_html(title: str, paragraphs: list[str]) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><body><h1>{title}</h1>{body}</body></html>"


class FakeHttpGet:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if url not in self.responses:
            raise core.HelpContentError(f"no fixture registered for {url}")
        return self.responses[url]


def _http_get_with_one_matching_article() -> FakeHttpGet:
    """Every category index is empty except 'meters', which links to the
    meter-reading article; the article page itself is a small real sample."""
    responses = {core.category_url(slug): _category_html([]) for slug in core.CATEGORY_SLUGS}
    responses[core.category_url("meters")] = _category_html([(METER_ARTICLE_URL, METER_ARTICLE_TITLE)])
    responses[METER_ARTICLE_URL] = _article_html(METER_ARTICLE_TITLE, ["There are a few different kinds of meters..."])
    return FakeHttpGet(responses)


def _http_get_with_no_matches() -> FakeHttpGet:
    return FakeHttpGet({core.category_url(slug): _category_html([]) for slug in core.CATEGORY_SLUGS})


class EndToEndDispatchTests(unittest.TestCase):
    def test_account_specific_redirects_without_touching_jira(self):
        client = FakeAnthropicClient([classify_response("account_specific")])
        result = core.handle_message("what's my balance?", client=client, jira_config=make_jira_config(),
                                      jira_request=_poison_request, jira_agile_request=_poison_request)
        self.assertEqual(result, {"type": "REDIRECT", "reason": "account_specific", "text": core.REDIRECT_TEXT})

    def test_dispute_redirects_without_touching_jira(self):
        client = FakeAnthropicClient([classify_response("dispute")])
        result = core.handle_message("I want to complain about my bill", client=client, jira_config=make_jira_config(),
                                      jira_request=_poison_request, jira_agile_request=_poison_request)
        self.assertEqual(result["reason"], "dispute")

    def test_off_topic_redirects_as_content_gap_shape_but_never_logs(self):
        client = FakeAnthropicClient([classify_response("off_topic")])
        result = core.handle_message("write me a poem", client=client, jira_config=make_jira_config(),
                                      jira_request=_poison_request, jira_agile_request=_poison_request)
        self.assertEqual(result["type"], "REDIRECT")
        self.assertEqual(result["reason"], "content_gap")

    def test_classification_failure_fails_safe_without_touching_jira(self):
        client = FakeAnthropicClient([FakeMessage([text_block("no tool use")])])
        result = core.handle_message("anything", client=client, jira_config=make_jira_config(),
                                      jira_request=_poison_request, jira_agile_request=_poison_request)
        self.assertEqual(result["reason"], "content_gap")

    def test_general_with_a_matching_page_returns_grounded_answer_and_never_logs(self):
        client = FakeAnthropicClient([
            classify_response("general", topic_query="how do I read my meter", topic_category_slugs=["meters"]),
            answer_response("Here's how to read your meter...", [METER_ARTICLE_URL]),
        ])
        http_get = _http_get_with_one_matching_article()
        result = core.handle_message("how do I read my meter?", client=client, http_get=http_get,
                                      jira_config=make_jira_config(), jira_request=_poison_request, jira_agile_request=_poison_request)
        self.assertEqual(result["type"], "GROUNDED_ANSWER")
        self.assertEqual(result["citations"], [METER_ARTICLE_URL])

    def test_general_with_no_candidate_pages_logs_a_content_gap(self):
        client = FakeAnthropicClient([
            classify_response("general", topic_query="completely unmatched zyzzyx topic"),
            redact_response([]),  # the free-text redaction pass, called before logging
        ])
        http_get = _http_get_with_no_matches()
        request, agile = FakeJiraRequest(), FakeJiraAgileRequest(issue_in_sprint=True)
        result = core.handle_message("completely unmatched zyzzyx topic", client=client, http_get=http_get,
                                      jira_config=make_jira_config(), jira_request=request, jira_agile_request=agile)
        self.assertEqual(result["reason"], "content_gap")
        self.assertTrue(any(m == "POST" and p == "issue" for m, p, _ in request.calls))

    def test_general_with_pages_but_model_says_insufficient_logs_a_content_gap(self):
        client = FakeAnthropicClient([
            classify_response("general", topic_query="how do I read my meter", topic_category_slugs=["meters"]),
            insufficient_response(),
            redact_response([]),
        ])
        http_get = _http_get_with_one_matching_article()
        request, agile = FakeJiraRequest(), FakeJiraAgileRequest(issue_in_sprint=True)
        result = core.handle_message("how do I read my meter?", client=client, http_get=http_get,
                                      jira_config=make_jira_config(), jira_request=request, jira_agile_request=agile)
        self.assertEqual(result["reason"], "content_gap")
        self.assertTrue(any(m == "POST" and p == "issue" for m, p, _ in request.calls))

    def test_content_gap_logging_is_skipped_silently_when_jira_unconfigured(self):
        client = FakeAnthropicClient([
            classify_response("general", topic_query="completely unmatched zyzzyx topic"),
            redact_response([]),
        ])
        http_get = _http_get_with_no_matches()
        result = core.handle_message("completely unmatched zyzzyx topic", client=client, http_get=http_get, jira_config=None)
        self.assertEqual(result["reason"], "content_gap")  # still a normal redirect, just not logged


if __name__ == "__main__":
    unittest.main()
