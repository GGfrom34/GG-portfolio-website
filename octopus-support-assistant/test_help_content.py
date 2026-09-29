"""Offline unit tests for core.py's help-content fetch/parse/retrieval layer.
Uses recorded real octopus.energy HTML (fixtures/help_fixtures.py) plus a
few small handcrafted HTML snippets for parser edge cases. No network.
"""

from __future__ import annotations

import unittest

import core
from fixtures.help_fixtures import Playback


# ---------------------------------------------------------------------------
# parse_help_html -- pure, no network. Handcrafted snippets for edge cases,
# then the recorded real article page as a realistic end-to-end check.
# ---------------------------------------------------------------------------

class ParseHelpHtmlTests(unittest.TestCase):
    def test_extracts_title_and_body_text_in_order(self):
        html = "<html><body><h1>My Title</h1><p>First paragraph.</p><h2>A heading</h2><p>Second paragraph.</p></body></html>"
        page = core.parse_help_html(html, "https://example.com/a/", fetched_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(page.title, "My Title")
        self.assertEqual(page.text, "First paragraph.\n\nA heading\n\nSecond paragraph.")
        self.assertEqual(page.url, "https://example.com/a/")

    def test_nested_inline_tags_inside_a_paragraph_stay_together(self):
        html = "<html><body><h1>T</h1><p>Click <a href='#'>here</a> to <strong>continue</strong>.</p></body></html>"
        page = core.parse_help_html(html, "u", fetched_at="now")
        self.assertEqual(page.text, "Click here to continue .")

    def test_script_style_nav_and_footer_are_excluded(self):
        html = (
            "<html><body>"
            "<nav>Site nav link</nav>"
            "<script>var x = 'should not appear';</script>"
            "<style>.a { color: red; }</style>"
            "<h1>T</h1><p>Real content.</p>"
            "<footer>Copyright footer text</footer>"
            "</body></html>"
        )
        page = core.parse_help_html(html, "u", fetched_at="now")
        self.assertEqual(page.text, "Real content.")
        self.assertNotIn("nav", page.text.lower())
        self.assertNotIn("footer", page.text.lower())
        self.assertNotIn("should not appear", page.text)

    def test_list_items_are_captured(self):
        html = "<html><body><h1>T</h1><ul><li>Step one</li><li>Step two</li></ul></body></html>"
        page = core.parse_help_html(html, "u", fetched_at="now")
        self.assertEqual(page.text, "Step one\n\nStep two")

    def test_missing_title_falls_back_to_the_url(self):
        html = "<html><body><p>No h1 here.</p></body></html>"
        page = core.parse_help_html(html, "https://example.com/x/", fetched_at="now")
        self.assertEqual(page.title, "https://example.com/x/")

    def test_real_recorded_article_page(self):
        playback = Playback.load()
        article_url = core.article_url("how-do-i-read-my-meter")
        html = playback(article_url)
        page = core.parse_help_html(html, article_url, fetched_at="now")
        self.assertIn("read my meter", page.title.lower())
        self.assertIn("meter reading reminder", page.text.lower())
        self.assertIn("why are meter readings so important", page.text.lower())
        # Chrome that should not leak into the parsed body text:
        self.assertNotIn("function(", page.text)  # no raw JS
        self.assertNotIn("__next_f", page.text)   # no RSC flight-data payload


# ---------------------------------------------------------------------------
# Category link parsing / fetch_category_index -- against the real recorded
# "meters" category page.
# ---------------------------------------------------------------------------

class CategoryIndexTests(unittest.TestCase):
    def test_parses_real_category_links(self):
        playback = Playback.load()
        links = core.fetch_category_index("meters", http_get=playback)
        self.assertGreater(len(links), 5)
        urls = [u for u, _ in links]
        self.assertIn(core.article_url("how-do-i-read-my-meter"), urls)
        titles = dict(links)
        self.assertIn("read my meter", titles[core.article_url("how-do-i-read-my-meter")].lower())

    def test_links_are_deduplicated(self):
        playback = Playback.load()
        links = core.fetch_category_index("meters", http_get=playback)
        urls = [u for u, _ in links]
        self.assertEqual(len(urls), len(set(urls)))

    def test_unrecorded_slug_raises_help_content_error(self):
        playback = Playback.load()
        with self.assertRaises(AssertionError):  # Playback's own "not recorded" guard
            core.fetch_category_index("solar", http_get=playback)


# ---------------------------------------------------------------------------
# _score -- pure token-overlap relevance
# ---------------------------------------------------------------------------

class ScoreTests(unittest.TestCase):
    def test_strong_overlap_scores_high(self):
        s = core._score("how do I read my meter", "How do I read my meter and submit a meter reading?")
        self.assertGreater(s, 0.9)

    def test_no_shared_tokens_scores_zero(self):
        self.assertEqual(core._score("switching tariff", "How do I read my meter?"), 0.0)

    def test_stopword_only_query_scores_zero_rather_than_crashing(self):
        self.assertEqual(core._score("how do I", "How do I read my meter?"), 0.0)


# ---------------------------------------------------------------------------
# search_help_pages -- the retrieval step, including a stale/missing slug
# being skipped rather than failing the whole search.
# ---------------------------------------------------------------------------

def _routed_http_get(playback: Playback, *, broken_slugs=()):
    broken_urls = {core.category_url(s) for s in broken_slugs}

    def _get(url: str) -> str:
        if url in broken_urls:
            raise core.HelpContentError(f"simulated failure for {url}")
        return playback(url)

    return _get


class SearchHelpPagesTests(unittest.TestCase):
    def test_finds_and_fetches_the_matching_real_article(self):
        playback = Playback.load()
        http_get = _routed_http_get(playback)
        pages = core.search_help_pages(
            "how do I read my meter", category_slugs=("meters",), http_get=http_get, max_pages=1,
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].url, core.article_url("how-do-i-read-my-meter"))
        self.assertIn("meter reading reminder", pages[0].text.lower())

    def test_a_failing_category_index_is_skipped_not_fatal(self):
        playback = Playback.load()
        http_get = _routed_http_get(playback, broken_slugs=("octoplus",))
        pages = core.search_help_pages(
            "how do I read my meter", category_slugs=("octoplus", "meters"), http_get=http_get, max_pages=1,
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].url, core.article_url("how-do-i-read-my-meter"))

    def test_no_matching_titles_returns_empty_list(self):
        playback = Playback.load()
        http_get = _routed_http_get(playback)
        pages = core.search_help_pages(
            "completely unrelated zyzzyx quantum topic", category_slugs=("meters",), http_get=http_get,
        )
        self.assertEqual(pages, [])


if __name__ == "__main__":
    unittest.main()
