"""Record and replay Octopus API responses for offline tests of core's API layer.

Playback (used by test_api.py):

    stub = Playback.load()
    with mock.patch.object(core, "_get_json", stub):
        market = core.fetch_market("C", now=NOW)

Recording (needs network; run only when you want to refresh the fixture):

    python api_fixtures.py

Recording wraps core._get_json, runs core.fetch_market for each region in
RECORD_REGIONS at the fixed time NOW, and saves every response keyed by the
exact URL requested. Because the fixture is made from what core actually asks
for, playback fails loudly if core later requests something not recorded.

Responses are trimmed to keep the file small: product details keep only the
recorded regions, and rate records keep only the fields core reads.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "api_responses.json"
NOW = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)  # BST; every recorded window is relative to this
RECORD_REGIONS = ("C", "N")
RECORD_POSTCODE = "SW1A 1AA"  # a lookup_region call is recorded too
RATE_FIELDS = ("value_inc_vat", "valid_from", "valid_to", "payment_method")


def fixture_key(url: str, params: Optional[dict[str, Any]] = None) -> str:
    """Canonical key for a request: the URL with query parameters sorted."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(sorted(params.items()))}"
    return url


class Playback:
    """A stand-in for core._get_json that serves recorded responses."""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[str] = []  # keys requested, in order

    @classmethod
    def load(cls, path: Path = FIXTURE_PATH) -> "Playback":
        return cls(json.loads(path.read_text(encoding="utf-8"))["responses"])

    def __call__(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        key = fixture_key(url, params)
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"core requested a URL that was not recorded: {key}\n(re-record with: python api_fixtures.py)")
        return json.loads(json.dumps(self.responses[key]))  # fresh copy so tests cannot corrupt the fixture


def _trim(key: str, data: Any) -> Any:
    path = urllib.parse.urlsplit(key).path
    if isinstance(data, dict) and "results" in data and "/standard-unit-rates/" in path:
        return {**data, "results": [{f: r.get(f) for f in RATE_FIELDS} for r in data["results"]]}
    if isinstance(data, dict) and "single_register_electricity_tariffs" in data:
        keep = {f"_{r}" for r in RECORD_REGIONS}
        fields = ("code", "full_name", "display_name", "direction", "is_variable", "is_prepay", "available_from", "available_to", "brand")
        trimmed = {f: data.get(f) for f in fields}
        trimmed["single_register_electricity_tariffs"] = {
            region: tariffs for region, tariffs in data["single_register_electricity_tariffs"].items() if region in keep
        }
        return trimmed
    return data


def record(path: Path = FIXTURE_PATH) -> int:
    import core  # imported here so playback users do not need the recorder's dependencies

    responses: dict[str, Any] = {}
    real_get_json = core._get_json

    def recording(url: str, params: Optional[dict[str, Any]] = None) -> Any:
        data = real_get_json(url, params)
        key = fixture_key(url, params)
        responses[key] = data
        return data

    core._get_json = recording
    try:
        for region in RECORD_REGIONS:
            core.fetch_market(region, now=NOW)
        core.lookup_region(RECORD_POSTCODE)
    finally:
        core._get_json = real_get_json

    payload = {
        "_meta": {
            "recorded_from": core.API_BASE,
            "now": NOW.isoformat(),
            "regions": list(RECORD_REGIONS),
            "postcode": RECORD_POSTCODE,
            "note": "Trimmed: product details keep only the recorded regions; rate records keep only the fields core reads.",
        },
        "responses": {k: _trim(k, v) for k, v in sorted(responses.items())},
    }
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Recorded {len(responses)} responses to {path} ({path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(record())
