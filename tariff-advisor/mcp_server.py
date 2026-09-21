"""MCP server for the Octopus Tariff Advisor.

A thin adapter over core.py, like cli.py: it maps typed tool arguments to a
profile dict, calls the core, and maps core errors to MCP tool errors. It holds
no tariff logic. Informational only; not financial or regulated switching advice.

Run over stdio (how Claude Desktop / Claude Code launch it):

    python mcp_server.py

Needs Python 3.10+ and `pip install -r requirements-mcp.txt`. core.py and cli.py
do not need either. stdout is the protocol channel in stdio mode, so this module
never prints; logging goes to stderr.
"""

import logging
import sys
import threading
import time
from typing import Annotated, Any, Callable, Literal, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

import core

log = logging.getLogger("octopus-tariff-advisor")

MARKET_CACHE_TTL_S = 600.0  # 10 minutes

# Kept explicit so the tool schema shows the model an enum. test_mcp_server.py asserts
# this matches core.REGIONS, so the two cannot drift apart.
RegionLetter = Literal["A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M", "N", "P"]
EvPattern = Literal["overnight", "daytime", "flexible"]

INSTRUCTIONS = (
    "Explores Octopus Energy tariffs for a UK household using live public rates. This is an "
    "informational aid, not financial advice or regulated switching advice, and it is not "
    "affiliated with Octopus Energy. All costs are estimates built on the assumptions returned "
    "in each result. When presenting a recommendation, include the key reasons, caveats and "
    "assumptions, say that figures are estimates, and suggest checking octopus.energy before "
    "any switch. The region is required: use find_region with a postcode, or list_regions, "
    "rather than guessing one."
)

_LOOKUP = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=True)
_OFFLINE = ToolAnnotations(read_only_hint=True, idempotent_hint=True, destructive_hint=False, open_world_hint=False)


class MarketCache:
    """Caches core.fetch_market per region for a short time.

    One recommendation costs about a dozen HTTP requests, and an MCP conversation calls it
    repeatedly. Tools run on worker threads, so access is guarded by a lock (which also
    stops concurrent callers fetching the same region twice). Failures are not cached.
    """

    def __init__(self, fetch: Callable[[str], core.Market] = core.fetch_market,
                 ttl_s: float = MARKET_CACHE_TTL_S, clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl_s, self._clock = fetch, ttl_s, clock
        self._entries: dict[str, tuple[float, core.Market]] = {}
        self._lock = threading.Lock()

    def __call__(self, region: str) -> core.Market:
        with self._lock:
            hit = self._entries.get(region)
            if hit and self._clock() - hit[0] < self._ttl_s:
                return hit[1]
            market = self._fetch(region)
            self._entries[region] = (self._clock(), market)
            return market


market_cache = MarketCache()

mcp = MCPServer("octopus-tariff-advisor", instructions=INSTRUCTIONS, version="0.1.0")


def _run(call: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call into the core, turning its errors into ToolErrors.

    A plain exception reaches the client with its message stripped (and a traceback in the
    log), so bad input must become a ToolError to keep hints such as the valid region letters.
    """
    try:
        return call(*args, **kwargs)
    except core.ProfileError as exc:
        raise ToolError(str(exc)) from exc
    except core.OctopusApiError as exc:
        log.warning("Octopus API problem: %s", exc)
        raise ToolError(f"Octopus rates are temporarily unavailable, so no result could be produced. Try again shortly. ({exc})") from exc


@mcp.tool(title="Recommend Octopus tariffs", annotations=_LOOKUP)
def recommend_tariff(
    region: Annotated[RegionLetter, Field(description="Electricity region letter A-P (C is London, N is Southern Scotland). Use find_region with a postcode if unknown.")],
    average_usage_kwh: Annotated[float, Field(gt=0, description="Annual electricity use in kWh for the whole household, including any EV charging.")],
    has_solar: Annotated[bool, Field(description="Whether the property has solar panels.")],
    has_ev: Annotated[bool, Field(description="Whether the household charges an electric vehicle at home.")],
    export_capacity_kw: Annotated[Optional[float], Field(gt=0, description="Solar array size in kW, if known (assumed 4 kW when has_solar and omitted).")] = None,
    ev_charging_pattern: Annotated[Optional[EvPattern], Field(description="Required when has_ev is true: 'overnight' (scheduled at night), 'daytime', or 'flexible' (can follow cheap half-hours).")] = None,
    ev_kwh_per_week: Annotated[Optional[float], Field(gt=0, description="Rough EV charging energy per week (assumed 40 kWh when omitted).")] = None,
    has_battery: Annotated[bool, Field(description="Whether the property has home battery storage.")] = False,
    battery_kwh: Annotated[Optional[float], Field(gt=0, description="Battery capacity in kWh (assumed 5 kWh when omitted).")] = None,
    battery_can_shift_to_offpeak: Annotated[Optional[bool], Field(description="Whether the battery can charge from the grid at cheap times and discharge at peak. Ask if unknown; assumed false when omitted.")] = None,
) -> dict[str, Any]:
    """Recommend the best-fit Octopus tariffs for a household and explain why.

    Compares Flexible Octopus (standard variable), Agile, Go and Cosy for import, and Outgoing
    and Agile Outgoing for solar export, using current public rates. Returns ranked
    recommendations with the rate that applies, reasons, caveats, the assumptions used and the
    data source. Informational only; not financial or regulated switching advice. Costs are estimates.
    """
    given = {
        "region": region, "average_usage_kwh": average_usage_kwh, "has_solar": has_solar, "has_ev": has_ev,
        "export_capacity_kw": export_capacity_kw, "ev_charging_pattern": ev_charging_pattern,
        "ev_kwh_per_week": ev_kwh_per_week, "has_battery": has_battery, "battery_kwh": battery_kwh,
        "battery_can_shift_to_offpeak": battery_can_shift_to_offpeak,
    }
    profile = {k: v for k, v in given.items() if v is not None}  # omitted stays omitted, so core can warn about assumed defaults
    # fetcher= (not market=) lets core validate the profile before any network call.
    return _run(lambda: core.recommend_tariff(profile, fetcher=market_cache))


@mcp.tool(title="Find electricity region from postcode", annotations=_LOOKUP)
def find_region(
    postcode: Annotated[str, Field(min_length=5, max_length=10, description="Full UK postcode, for example 'SW1A 1AA'.")],
) -> dict[str, Any]:
    """Find the electricity region letter for a UK postcode, for use with recommend_tariff.

    Sends the postcode to Octopus's public grid-supply-points lookup (nothing is stored). If the
    postcode straddles two regions, `ambiguous` is true and `region` is null: ask the household
    which applies rather than guessing.
    """
    return _run(core.lookup_region, postcode)


@mcp.tool(title="List electricity regions", annotations=_OFFLINE)
def list_regions() -> dict[str, str]:
    """List the region letters (A-P) and their names, as used by recommend_tariff."""
    return dict(core.REGIONS)


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
