"""Record and replay real octopus.energy help-page HTML for offline tests of
core's fetch/parse/retrieval layer.

Playback (used by test_help_content.py):

    stub = Playback.load()
    pages = core.fetch_category_index("meters", http_get=stub)

Recording (needs network; run only when you want to refresh the fixture):

    python fixtures/help_fixtures.py

Unlike tariff-advisor's JSON API responses, this is raw page HTML, which
can't be trimmed without breaking the parser tests it exists to exercise
(the point is testing parse_help_html/_parse_category_article_links against
real, messy markup) -- so only a small, representative sample is recorded:
one category page and one article page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE_PATH = Path(__file__).parent / "help_pages.json"
RECORD_CATEGORY_SLUGS = ("meters",)
RECORD_ARTICLE_SLUGS = ("how-do-i-read-my-meter",)


class Playback:
    """A stand-in for core._http_get that serves recorded HTML."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[str] = []  # URLs requested, in order

    @classmethod
    def load(cls, path: Path = FIXTURE_PATH) -> "Playback":
        return cls(json.loads(path.read_text(encoding="utf-8"))["responses"])

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"core requested a URL that was not recorded: {url}\n(re-record with: python fixtures/help_fixtures.py)")
        return self.responses[url]


def record(path: Path = FIXTURE_PATH) -> int:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import core  # imported here so playback users do not need network access

    responses: dict[str, str] = {}

    def recording(url: str) -> str:
        html = core._http_get(url)
        responses[url] = html
        return html

    for slug in RECORD_CATEGORY_SLUGS:
        core.fetch_category_index(slug, http_get=recording)
    for slug in RECORD_ARTICLE_SLUGS:
        core.fetch_help_page(core.article_url(slug), http_get=recording)

    payload = {
        "_meta": {
            "recorded_from": "https://octopus.energy",
            "category_slugs": list(RECORD_CATEGORY_SLUGS),
            "article_slugs": list(RECORD_ARTICLE_SLUGS),
            "note": "Raw page HTML, unmodified -- kept small by recording only one category and one article page.",
        },
        "responses": responses,
    }
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    total_kb = sum(len(v) for v in responses.values()) / 1024
    print(f"Recorded {len(responses)} pages to {path} ({total_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(record())
