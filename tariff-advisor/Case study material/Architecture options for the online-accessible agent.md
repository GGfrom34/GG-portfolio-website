# Public "Ask the Tariff Advisor" web chat (Option 1: direct-import backend)

## Context

The tariff advisor today has two interfaces over `core.py`'s `recommend_tariff()`: a CLI (`cli.py`) and a stdio MCP server (`mcp_server.py`) used inside Claude Desktop/Code. The user wants a third interface: a public chat widget on the portfolio site so a visitor can talk to "the tariff advisor" without opening Claude Code themselves.

Chosen architecture (confirmed with the user): a new thin Python backend (`web_server.py`) that imports `core.py` directly and drives the Anthropic Messages API's tool-use loop itself — no MCP transport involved for this interface. It follows the same "thin adapter, no tariff logic" discipline as `cli.py`/`mcp_server.py`. The portfolio site's frontend stays static; the backend is a small separately-hosted service.

Two small, justified refactors keep this DRY with `mcp_server.py` rather than copy-pasting behavior:
- Move the `INSTRUCTIONS` string (the "answer first, then offer to refine" behavioral contract) from `mcp_server.py` into `core.py` as `core.AGENT_INSTRUCTIONS`, a plain string constant. Both adapters build their system prompt from it; `web_server.py` wraps it with its own persona preamble, `mcp_server.py` uses it unchanged. This is behavior-preserving — `mcp_server.py`'s `INSTRUCTIONS` becomes `core.AGENT_INSTRUCTIONS` with identical text.
- Move `MarketCache` from `mcp_server.py` into `core.py` (it's generic TTL-caching over `core.fetch_market`, nothing MCP-specific). Both `mcp_server.py` and `web_server.py` import `core.MarketCache`.

Everything else — tool JSON schema, session handling, rate limiting, SSE streaming, persona — lives only in `web_server.py`, following the existing convention of hand-declaring enums per-adapter and asserting them against `core.REGIONS` in tests (the same pattern `test_mcp_server.py` already uses for `RegionLetter`).

## Backend: `tariff-advisor/web_server.py`

FastAPI app (new dependency, not shared with the MCP extra — a new `requirements-web.txt`, mirroring how `requirements-mcp.txt` is scoped to `mcp_server.py` only). Dependencies: `fastapi`, `uvicorn[standard]`, `anthropic`. No new rate-limiting library — a small hand-rolled in-memory per-IP token bucket, since the scale doesn't justify one.

- `POST /chat` — body `{session_id: str | null, message: str}`. Streams the assistant's reply as Server-Sent Events (`text` deltas, a `status` event while a tool call is in flight, a terminal `done` event carrying the `session_id` a first-time caller should persist).
- `GET /health` — plain 200, for the hosting platform's health check.
- Session store: `OrderedDict[str, list[MessageParam]]` capped at a fixed size (e.g. 500), evicting oldest on overflow — enough for a demo-scale single-instance service. Documented as a known limitation (lost on restart, not multi-instance safe) rather than reached for Redis.
- Tool-use loop: call `anthropic_client.messages.create(..., stream=True)`; on `tool_use` for `recommend_tariff`, map given args to a profile dict exactly like `mcp_server.recommend_tariff` does today (omit unset fields so `core.recommend_tariff` reports them as assumed), call `core.recommend_tariff(profile, fetcher=market_cache, allow_assumptions=True)`, catch `core.ProfileError` / `core.OctopusApiError` and return an `is_error=True` tool_result with the message (same intent as `mcp_server._run`'s `ToolError` mapping). Loop until `stop_reason != "tool_use"`.
- Tool JSON schema: hand-written to match `recommend_tariff`'s current MCP parameters (region enum from `core.REGIONS`, ev pattern enum, the rest optional numbers/bools). A new `test_web_server.py` asserts the enums match `core.REGIONS`, mirroring `test_mcp_server.py::test_region_letter_enum_matches_the_core`.
- System prompt = a short persona preamble (name, tone, scope-fencing: only answers Octopus-tariff questions, redirects anything else) + `core.AGENT_INSTRUCTIONS`.
- Anthropic client and model name are injectable (module-level, overridable) so tests can pass a stub client returning canned tool_use/text blocks — no real API calls or spend in tests, same boundary-stubbing approach `test_api.py`/`test_mcp_server.py` already use for the Octopus API.
- Guardrails (tightened per the user's request for a hard ceiling on spend):
  - **Global daily token budget: 10,000 tokens/day (input+output combined) across all visitors**, env-configurable (`GLOBAL_DAILY_TOKEN_BUDGET`). A `TokenBudget` tracker persists its counters to a small local file (`.budget_state.json`, one write per Anthropic call) so a service restart mid-day does not reset the clock and silently reopen the spend — an in-memory-only counter would defeat the guarantee on any host that restarts or redeploys. Checked *before every Anthropic API call* (not just once per `/chat` request), using a conservative reserved estimate (input tokens so far + `max_tokens`); decremented after the call using the actual `usage.input_tokens + usage.output_tokens` from the response, so reserved-but-unused tokens are returned to the budget. Resets at UTC midnight.
  - **Per-IP daily token share: 2,000 tokens/day** (env-configurable, `PER_IP_DAILY_TOKEN_BUDGET`), same persisted-counter mechanism, so a single visitor cannot consume the whole global budget alone (leaves room for roughly 5 distinct visitors/day at the defaults).
  - **Per-IP rate limit: 5 messages/minute** (down from the earlier draft's 10), in-memory token bucket — this is a burst/abuse-speed control, separate from the token-budget controls above.
  - Max input length **1,000 characters** (down from 2,000), max **12 tool-loop turns per session** (down from 20), `max_tokens` **capped at 400 per call** (down from 1024) — all env-overridable but shipped tight by default.
  - When the global or per-IP budget is exhausted, `/chat` returns a clear, non-technical message ("today's demo budget is used up, try again tomorrow, or read the write-up / browse the code instead") rather than a bare error, since this is expected to happen by design, not a fault condition.
  - CORS restricted to an `ALLOWED_ORIGIN` env var. `ANTHROPIC_API_KEY` from environment only. No full message/postcode bodies logged at INFO level (mirrors the MCP server's existing privacy note).

## Frontend: chat widget

- New static assets: `case-studies/tariff-advisor-widget.js`, and widget styles appended to the existing shared `styles.css` (the site has one stylesheet for all pages — no new per-page CSS file, to match current convention).
- Embedded into `case-studies/octopus-tariff-advisor.html` as a new `<section id="ask-the-advisor">` (that page is currently placeholder content for Problem/Approach/Capabilities/Outcome/Repo — this plan only adds the live widget section, not the case-study prose, which is separate work).
- Vanilla JS, no framework, consistent with the rest of the site: `fetch(BACKEND_URL + "/chat", {method: "POST", ...})`, reads the SSE-shaped stream via the response body's `ReadableStream` (not `EventSource`, since that only supports GET), renders streamed text, keeps `session_id` in `sessionStorage` so a page reload continues the conversation, has a visible "new conversation" reset. Shows the existing disclaimer framing ("informational only, not financial advice") near the widget plus a short note that messages go to a backend service.
- `BACKEND_URL` is a constant at the top of the widget JS, pointed at the deployed backend.

## Tests

`test_web_server.py`, offline (no network, no real Anthropic calls), following the existing suites' conventions:
- Region/EV-pattern enum matches `core.REGIONS` (mirrors the existing MCP test).
- Profile mapping from tool args matches what `core.recommend_tariff` receives (via the recorded fixtures already in `fixtures/api_responses.json` / `api_fixtures.py`, same as `test_mcp_server.py`).
- `ProfileError` / `OctopusApiError` map to `is_error=True` tool results with the message intact.
- Rate limiter rejects over-quota requests; session cap evicts oldest.
- `TokenBudget`: reserve/record/reset arithmetic, global cap blocks further calls once exhausted regardless of which IP, per-IP cap blocks that IP while others can still proceed (until the global cap also trips), persisted state survives a simulated restart (reload from file mid-day keeps the correct remaining balance), and resets at UTC midnight.
- CORS header reflects `ALLOWED_ORIGIN`.
- Architecture check: `core.py` still imports neither `fastapi` nor `anthropic` (mirrors `test_core_and_cli_do_not_import_mcp_or_pydantic`).
- The two refactors don't regress `test_mcp_server.py` (instructions text and cache behavior unchanged, just relocated).

## README

Add a "Web chat (public site)" section to `tariff-advisor/README.md`, same structure/depth as the existing MCP section: what it does, requirements (`requirements-web.txt`), env vars (`ANTHROPIC_API_KEY`, `ALLOWED_ORIGIN`, rate-limit overrides), how to run locally (`uvicorn web_server:app --reload`), and a suggested deploy target (Render, as a persistent-process Python host so the in-memory session/market cache survive across requests) with a `Procfile`.

## Verification

1. `python -m unittest -v test_core test_api test_mcp_server test_web_server` — existing suites stay green after the two refactors, new suite passes offline.
2. Run `web_server.py` locally with a real `ANTHROPIC_API_KEY`; open the case-study page locally, exercise the widget end-to-end: a vague first message gets an answer plus the assumed-inputs questions (mirrors the README's documented "first answer, then refine" flow), a follow-up narrows the assumptions, an invalid input (e.g. bad region) surfaces a clear error.
3. Confirm rate limiting kicks in past the configured threshold, that the global 10,000-token daily budget and the 2,000-token per-IP share both hard-stop `/chat` with a friendly message once exhausted, that the budget survives a process restart (persisted file), and that `core.py` and `cli.py` still have zero knowledge of FastAPI/Anthropic (existing `ArchitectureTests` pattern, extended).
