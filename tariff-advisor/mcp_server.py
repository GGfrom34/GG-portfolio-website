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
EvPattern = Literal["overnight", "mixed", "daytime", "flexible"]

INSTRUCTIONS = (
    "Explores Octopus Energy tariffs for a UK household using live public rates. This is an "
    "informational aid, not financial advice or regulated switching advice, and it is not "
    "affiliated with Octopus Energy. All costs are estimates built on stated assumptions.\n\n"
    "How to behave:\n"
    "1. Answer first. After the user's first message, always call recommend_tariff straight away, "
    "even when inputs are missing. Pass only what the user actually said and leave everything else "
    "out: never invent values, and do not ask questions before answering. If they give a postcode, "
    "call find_region first and pass its region.\n"
    "2. Present the recommendation and say plainly which assumptions it rests on "
    "(see based_on_assumptions and assumed_inputs in the result).\n"
    "3. End that answer by offering a more accurate estimate: one short sentence inviting the user to "
    "replace the assumptions, followed by a bullet list of the `question` of EVERY entry in "
    "assumed_inputs, each once and in the order given. Do not add questions that are not in the list "
    "and do not leave any out.\n"
    "4. When the user replies with more detail, call recommend_tariff again with everything known so "
    "far, and again end by listing only the questions still in assumed_inputs. If assumed_inputs is "
    "empty, do not make the offer.\n"
    "5. Always say the figures are estimates, include the key reasons and caveats, and suggest "
    "checking octopus.energy before any switch."
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

mcp = MCPServer("octopus-tariff-advisor", instructions=INSTRUCTIONS, version="0.2.0")


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
    region: Annotated[Optional[RegionLetter], Field(description="Electricity region letter A-P (C is London, N is Southern Scotland). Use find_region with the postcode. Omit if unknown.")] = None,
    average_usage_kwh: Annotated[Optional[float], Field(gt=0, description="Total annual electricity use in kWh for the whole household, including any EV charging. Omit if unknown.")] = None,
    has_solar: Annotated[Optional[bool], Field(description="Whether the property has solar panels. Omit unless the user said.")] = None,
    has_ev: Annotated[Optional[bool], Field(description="Whether the household charges an electric vehicle at home. Omit unless the user said.")] = None,
    solar_kwp: Annotated[Optional[float], Field(gt=0, description="Size of the solar array in kWp. Omit if unknown.")] = None,
    ev_annual_kwh: Annotated[Optional[float], Field(gt=0, description="Annual EV charging consumption in kWh. Omit if unknown.")] = None,
    ev_charging_pattern: Annotated[Optional[EvPattern], Field(description="How the EV is charged: 'overnight' (off-peak only), 'mixed' (a mix of off-peak and peak), 'daytime', or 'flexible' (can follow cheap half-hours). Omit if unknown.")] = None,
    has_battery: Annotated[Optional[bool], Field(description="Whether the property has home battery storage. Omit unless the user said.")] = None,
    battery_kwh: Annotated[Optional[float], Field(gt=0, description="Home battery size in kWh. Omit if unknown.")] = None,
    battery_can_shift_to_offpeak: Annotated[Optional[bool], Field(description="Whether the battery can charge from the grid at cheap times and discharge at peak. Omit if unknown.")] = None,
) -> dict[str, Any]:
    """Recommend the best-fit Octopus tariffs for a household and explain why, even from a partial description.

    Call this straight away with whatever the user has said: every input is optional, and anything
    omitted is filled in with a stated assumption. Never invent values. The result lists each
    assumption in `assumed_inputs` with the `question` that would replace it: end your answer by
    offering a more accurate estimate and listing all of those questions.

    Compares Flexible Octopus (standard variable), Agile, Go and Cosy for import, and Outgoing
    and Agile Outgoing for solar export, using current public rates. Returns ranked
    recommendations with the rate that applies, reasons, caveats, the assumptions used and the
    data source. Informational only; not financial or regulated switching advice. Costs are estimates.
    """
    given = {
        "region": region, "average_usage_kwh": average_usage_kwh, "has_solar": has_solar, "has_ev": has_ev,
        "solar_kwp": solar_kwp, "ev_charging_pattern": ev_charging_pattern, "ev_annual_kwh": ev_annual_kwh,
        "has_battery": has_battery, "battery_kwh": battery_kwh, "battery_can_shift_to_offpeak": battery_can_shift_to_offpeak,
    }
    profile = {k: v for k, v in given.items() if v is not None}  # omitted stays omitted, so the core reports it as assumed
    # fetcher= (not market=) lets core validate the profile before any network call.
    return _run(lambda: core.recommend_tariff(profile, fetcher=market_cache, allow_assumptions=True))


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
