# Octopus Support Assistant

An **independent, unofficial customer-support chat assistant** for Octopus Energy customers. Given a customer's message, it either answers using content it actually fetched from Octopus's live public help pages just now (with the real page cited), or redirects to Octopus's real channels — for anything needing a real account, any dispute, or a genuine gap in what's been fetched.

> **This is not affiliated with, endorsed by, or operated by Octopus Energy**, and it has no access to any real customer account. It's a demo built to explore grounded, boundary-respecting support agents: it never answers from background knowledge about Octopus, only from pages it fetched this run, and it treats "needs a real account" and "this is a dispute" as permanent boundaries rather than things to work around.

## How it is organised

Split into core logic and an interface, the same way as [`tariff-advisor`](../tariff-advisor/):

| File | Role |
|------|------|
| `core.py` | All the logic: fetching and parsing Octopus's help pages, classifying a message, drafting a grounded answer, redacting text, and logging content gaps to Jira. It has no knowledge of the command line or of any other interface — no printing, prompting, or exiting. The single entry point is `handle_message(message, history) -> dict`. |
| `cli.py` | A thin command-line REPL. It prints the opening disclosure, reads messages, calls `core.handle_message()`, and prints the result. It is the only file that touches stdin/stdout, and it never imports `anthropic`/`fastapi`/`web_server` or reads an environment variable itself — `core.py` owns all of that (enforced by `ArchitectureTests` in `test_core.py`/`test_web_server.py`). |
| `web_server.py` | A second interface: a small FastAPI backend that lets a visitor to the public portfolio site chat with the assistant directly, streaming `core.handle_message()`'s result to the browser as Server-Sent Events. See [Web chat](#web-chat-public-site). |

Unlike `tariff-advisor/core.py` (which never imports `anthropic` — the model is a UI-layer choice there), classification, redaction, and answer drafting **are** this tool's domain logic, so `core.py` here calls the Anthropic Messages API directly — the same way it calls Octopus's help pages directly. Every external call (the Anthropic client, the HTTP fetcher, the Jira request functions) is dependency-injected with a real default, so the whole test suite runs offline against fakes. `handle_message()` also accepts an `on_usage` callback (invoked once per internal model call with real token counts) and an `index_fetch` override (for a caller that lives across many messages, e.g. `web_server.py`, to avoid refetching every help category on every message) — both additive, so `cli.py` and every existing caller work unchanged.

## How a message is handled

1. **Classify first, as an explicit, separate step** (`classify_message`) — never left for the answer-drafting call to decide implicitly. A forced tool-use call sorts the message into one of four categories: `account_specific` (needs a real account — balance, tariff, billing history, submitting a meter reading, switching, personal details), `dispute` (any complaint), `off_topic` (not about Octopus/energy at all), or `general` (a genuine Octopus support question).
2. **`account_specific` and `dispute`** get a fixed `REDIRECT`, immediately, with no further model calls.
3. **`off_topic`** gets the same `REDIRECT` shape (`reason="content_gap"`) — but is never logged; it isn't a real gap in Octopus's help content, just an unrelated request.
4. **`general`** triggers retrieval (`search_help_pages`): every Octopus help category is scored against the message, and the best-matching article pages are fetched fresh. If nothing scores above zero, that's a content gap.
5. If pages were found, `generate_grounded_answer` drafts an answer **using only that fetched text** — a forced tool-use call that must say `insufficient` rather than guess if the text doesn't actually answer the question. Any citation it returns is checked against the URLs actually fetched this run; a citation outside that set is rejected outright, so a hallucinated URL is structurally impossible, not just discouraged by the prompt.
6. Whenever the model can't produce a grounded answer (no candidate pages, or it says `insufficient`), that's a **genuine content gap**: the question is redacted and logged to Jira for review, and the customer gets the same fixed `REDIRECT` text as every other reason.

The `REDIRECT` text is a single fixed string (`core.REDIRECT_TEXT`), identical for all three reasons and never model-generated, so nothing implies a request is "being handled."

## Redaction

Applied to anything before it reaches the Jira log (`redact_text`), in two passes:

1. `redact_patterns` — a pure regex pass: emails, UK phone numbers, UK postcodes, and a heuristic for Octopus-style account/meter reference numbers. Deliberately permissive (any 6–13 digit run is treated as a possible reference number), so it will also redact an ordinary large figure like a yearly kWh total — a documented false positive, not a bug.
2. `find_identifying_substrings` — a small-model (Haiku) call that catches identifying detail written as prose (a name or address in a sentence) that patterns would miss. It returns *exact substrings to redact*, never a rewritten text, and any substring that doesn't appear verbatim in the input is dropped — so the replacement stays deterministic and auditable, and a hallucinated span can't corrupt the text.

Both are directly unit-tested with synthetic PII in `test_core.py` (`RedactPatternsTests`, `FindIdentifyingSubstringsTests`, `RedactTextTests`), including honestly-documented cases of what gets through (e.g. a bare, context-free nickname).

## Jira content-gap logging

Content-gap issues are filed into the **same Jira project the [`prd-to-jira`](../prd-to-jira/) case study uses** (`SCRUM` / "Project GG"), inside a dedicated **"Escalation - support agent" sprint** on that project's board — per the project owner's direction, so this tool's Jira write activity stays visibly scoped to one sprint rather than spinning up a separate project.

This is a standalone script, not a Claude Code session, so it can't use the OAuth-authenticated Atlassian remote MCP server the way `prd-to-jira`'s `jira-epic-builder` agent does (see [`prd-to-jira`'s architecture notes](../prd-to-jira/Case%20study%20material/prd-to-jira-architecture.md)). Instead `core.py` calls the Jira Cloud REST API directly over `urllib` — Basic Auth with an email + API token, the same timeout/retry shape as the help-content fetch layer.

- `create_content_gap_issue` — the issue's summary and description contain **only the redacted question**, nothing else identifying.
- `add_issue_to_sprint` — files the new issue into the configured sprint via the Agile REST API.
- Both are then **read back** (`read_back_issue_summary`, `_issue_in_sprint`) to confirm what Jira actually stored and that the issue really landed in the sprint, rather than trusting the create/add calls' own responses.
- Any failure at any step is swallowed inside `log_content_gap`, which returns `None` and only logs a warning internally — a Jira outage must never surface to the customer or block the `REDIRECT` response.
- If `JIRA_SITE_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN`/`JIRA_PROJECT_KEY`/`JIRA_SPRINT_ID` aren't all set, logging is silently disabled (`jira_config_from_env()` returns `None`) — `handle_message` still works, it just never writes to Jira.

## Requirements

Python 3.10+ and the `anthropic` package (`requirements.txt`); an `ANTHROPIC_API_KEY` is required to run `cli.py` at all. Jira logging is optional — see `.env.example` for the five `JIRA_*` variables.

```bash
python -m venv .venv
# Windows:      .venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS/Linux:  .venv/bin/python -m pip install -r requirements.txt
```

## Usage

```bash
export ANTHROPIC_API_KEY=sk-...     # Windows: set ANTHROPIC_API_KEY=sk-...
python cli.py
```

```
python cli.py --once "How do I read my meter?"   # a single scripted exchange
python cli.py --once "..." --json                # print the raw response dict
```

### Using the core directly

```python
from core import handle_message

result = handle_message("How do I read my meter?", history=[])
print(result["type"])  # "GROUNDED_ANSWER" or "REDIRECT"
```

## Web chat (public site)

`web_server.py` is a small FastAPI backend that lets a visitor to the portfolio site chat with the assistant directly. It calls `core.handle_message()` **once per `/chat` request** and streams the result to the browser as Server-Sent Events. Unlike `tariff-advisor/web_server.py` (which drives the Anthropic tool-use loop itself and can stream token-by-token), `core.handle_message()` is a single opaque call with no incremental output, so there is no tool-use loop to drive here and nothing to stream word-by-word — one `status` event ("Thinking…") while the (few-second) call is in flight, then a single `text` event with the whole reply (citation URLs appended as `Source: <url>` lines, mirroring `cli.py`'s own `render()`), then `done`. Keeping the same three SSE event names as `tariff-advisor-widget.js` meant the frontend (`case-studies/support-assistant-widget.js`) needed almost no logic changes from `tariff-advisor-widget.js` — only constants and copy.

**This is a public, unauthenticated endpoint, so it is deliberately mean with money**, the same way `tariff-advisor/web_server.py` is. `RateLimiter`/`TokenBudget`/the session store are duplicated-and-adapted from that file, not imported — these are two independently-deployable Render services with no shared package. Guardrails, all env-overridable:

| Guardrail | Default | Purpose |
|---|---|---|
| `GLOBAL_DAILY_TOKEN_BUDGET` | 150,000 tokens/day | A hard ceiling across every visitor combined. Persisted to a small local JSON file so a restart or redeploy mid-day doesn't reopen the budget. |
| `PER_IP_DAILY_TOKEN_BUDGET` | 30,000 tokens/day | Stops one visitor from consuming the whole global budget — roughly one short conversation per visitor per day. |
| `PER_IP_RATE_LIMIT_PER_MIN` | 5 messages/minute | A burst limiter, separate from the token budgets above. |
| `MAX_INPUT_CHARS` | 1,000 characters | Per message. |
| `MAX_TURNS_PER_SESSION` | 8 tool-loop turns | Lower than tariff-advisor's 12 — a turn here costs more (see below). |
| `MAX_TOKENS_RESERVE_PER_MESSAGE` | 12,000 | The pre-flight `can_afford()` reserve for one whole `handle_message()` call (not a per-call cap — `core.py`'s functions already set their own `max_tokens`). Named differently from tariff-advisor's `MAX_TOKENS_PER_CALL` since a message here can cost up to 3 internal model calls, not 1. |

Per-message cost is larger and more variable than tariff-advisor's single bounded call, dominated by `generate_grounded_answer`'s prompt embedding up to 3 full fetched-article bodies: a worst-case turn (classify + an "insufficient" answer + the redaction call) runs to roughly 10,000 tokens, a typical successful answer to roughly 7,500. **These are starting points, not calibrated numbers** — tune both constants post-deploy against real usage (`token_budget.record()`'s totals come from `core.py`'s `on_usage` callback, which reports actual `response.usage` from every model call it makes).

Both token budgets are checked *before* every `handle_message()` call (using `MAX_TOKENS_RESERVE_PER_MESSAGE` as the reserved estimate) and reconciled against the real accumulated `on_usage` total afterwards. Once either is exhausted, `/chat` returns a plain, non-technical message rather than an error, since running out is expected by design on a demo budget. Sessions are an in-memory, single-instance store (capped, oldest evicted first) — conversations and the rate limiter (though not the persisted token budgets) are lost on restart and aren't shared across multiple instances.

**Cancellation is a known, accepted gap**: unlike tariff-advisor's potentially-many-tool-call loop, `handle_message()` has no internal yield points, so `/cancel` can stop a *future* turn on an abandoned session but can't interrupt one already in flight — its tokens are already spent by the time cancellation is noticed. Turns are short (≤3 quick calls), so this is an acceptable trade-off for now.

### Install and run

```bash
python -m venv .venv   # or reuse the one from requirements.txt
# Windows:      .venv\Scripts\python.exe -m pip install -r requirements-web.txt
# macOS/Linux:  .venv/bin/python -m pip install -r requirements-web.txt
```

```bash
export ANTHROPIC_API_KEY=sk-...          # Windows: set ANTHROPIC_API_KEY=sk-...
export ALLOWED_ORIGIN=http://localhost:8000   # the origin the widget is served from
uvicorn web_server:app --reload
```

### Deploying

A `Procfile` is included (`web: uvicorn web_server:app --host 0.0.0.0 --port $PORT`), and the root `render.yaml` has an `octopus-support-assistant` service entry alongside tariff-advisor's. Set `ANTHROPIC_API_KEY`/`ALLOWED_ORIGIN` (and, optionally, the five `JIRA_*` vars to enable live content-gap logging from the deployed demo) on the host, then point `BACKEND_URL` at the top of `case-studies/support-assistant-widget.js` at the deployed URL. The persisted token-budget file (`.budget_state.json`, overridable via `BUDGET_STATE_PATH`) needs to live on a writable, persistent disk — on Render's free tier (no persistent disk, spins down after ~15 min idle), `.github/workflows/keep-octopus-support-assistant-warm.yml` pings `/health` every 10 minutes to stop that idle window from silently resetting the budget, the same way tariff-advisor's own keep-warm workflow does.

## Tests

```bash
.venv/Scripts/python.exe -m unittest -v test_core test_help_content test_web_server   # offline: no network, no tokens, no Jira calls
```

| File | What it tests |
|------|--------------|
| `test_core.py` | Classification, redaction (with synthetic PII), Jira logging, grounded-answer validation, `on_usage` accounting, and `handle_message`'s full dispatch logic — all against a `FakeAnthropicClient` (mirroring `tariff-advisor/test_web_server.py`'s pattern) and fake HTTP/Jira request functions. Includes an `ArchitectureTests` check that `cli.py` never imports `anthropic` or reads the environment. |
| `test_help_content.py` | The fetch/parse/retrieval layer against **recorded real octopus.energy HTML** (`fixtures/help_fixtures.py`, mirroring `tariff-advisor/api_fixtures.py`'s record/replay pattern) plus handcrafted HTML for parser edge cases (nested inline tags, script/nav/footer exclusion). |
| `test_web_server.py` | The web adapter: guardrails, SSE framing, session handling, and cancellation against a faked `core.handle_message` (`ChatEndpointTests`), `RateLimiter`/`TokenBudget` near-verbatim ports of tariff-advisor's own tests, and an `EndToEndIntegrationTests` tier that drives `/chat` through the *real* `core.handle_message` (a `FakeAnthropicClient` plus the recorded help-page fixture) to prove the wiring — not just that `web_server` calls a stub correctly. Skipped automatically when `fastapi`/`anthropic`/`httpx` aren't installed. |

Re-record the help-page fixture (needs network) with `python fixtures/help_fixtures.py`.

See [`TESTING.md`](TESTING.md) for six real adversarial conversations run live against the model, real fetches, and a real Jira issue.

## Known limitations

- **Retrieval is a crude, stdlib-only token-overlap score** against article *titles* (no embeddings), so a genuinely covered topic phrased very differently from its article's title can be missed. This is deliberately permissive rather than strict — `generate_grounded_answer` is the real, fails-closed gate on whether a match actually supports an answer, so a weak retrieval match is safe (it just gets a chance to be rejected by the drafting step) but a missed one still produces an avoidable content-gap redirect.
- **The redaction pattern pass over-redacts long numbers**: any 6–13 digit run is treated as a possible account/meter reference, so an ordinary large figure (e.g. an annual kWh total) gets redacted too. Chosen deliberately over the alternative of under-redacting a real reference number.
- `JIRA_SPRINT_ID` is a static numeric ID, resolved once; if the "Escalation - support agent" sprint is ever deleted and recreated, the ID needs re-resolving.
- **`web_server.py` can't interrupt a turn already in flight** (see the cancellation note above) — accepted given turns are short.
