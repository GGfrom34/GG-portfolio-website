# Octopus Support Assistant

An **independent, unofficial customer-support chat assistant** for Octopus Energy customers. Given a customer's message, it either answers using content it actually fetched from Octopus's live public help pages just now (with the real page cited), or redirects to Octopus's real channels — for anything needing a real account, any dispute, or a genuine gap in what's been fetched.

> **This is not affiliated with, endorsed by, or operated by Octopus Energy**, and it has no access to any real customer account. It's a demo built to explore grounded, boundary-respecting support agents: it never answers from background knowledge about Octopus, only from pages it fetched this run, and it treats "needs a real account" and "this is a dispute" as permanent boundaries rather than things to work around.

## How it is organised

Split into core logic and an interface, the same way as [`tariff-advisor`](../tariff-advisor/):

| File | Role |
|------|------|
| `core.py` | All the logic: fetching and parsing Octopus's help pages, classifying a message, drafting a grounded answer, redacting text, and logging content gaps to Jira. It has no knowledge of the command line or of any other interface — no printing, prompting, or exiting. The single entry point is `handle_message(message, history) -> dict`. |
| `cli.py` | A thin command-line REPL. It prints the opening disclosure, reads messages, calls `core.handle_message()`, and prints the result. It is the only file that touches stdin/stdout, and it never imports `anthropic` or reads an environment variable itself — `core.py` owns all of that (enforced by `ArchitectureTests` in `test_core.py`). |

Unlike `tariff-advisor/core.py` (which never imports `anthropic` — the model is a UI-layer choice there), classification, redaction, and answer drafting **are** this tool's domain logic, so `core.py` here calls the Anthropic Messages API directly — the same way it calls Octopus's help pages directly. Every external call (the Anthropic client, the HTTP fetcher, the Jira request functions) is dependency-injected with a real default, so the whole test suite runs offline against fakes.

A future session adds a real web chat interface over the same `core.py`, the way `tariff-advisor` grew from CLI to MCP server to web chat.

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

## Tests

```bash
.venv/Scripts/python.exe -m unittest -v test_core test_help_content   # offline: no network, no tokens, no Jira calls
```

| File | What it tests |
|------|--------------|
| `test_core.py` | Classification, redaction (with synthetic PII), Jira logging, grounded-answer validation, and `handle_message`'s full dispatch logic — all against a `FakeAnthropicClient` (mirroring `tariff-advisor/test_web_server.py`'s pattern) and fake HTTP/Jira request functions. Includes an `ArchitectureTests` check that `cli.py` never imports `anthropic` or reads the environment. |
| `test_help_content.py` | The fetch/parse/retrieval layer against **recorded real octopus.energy HTML** (`fixtures/help_fixtures.py`, mirroring `tariff-advisor/api_fixtures.py`'s record/replay pattern) plus handcrafted HTML for parser edge cases (nested inline tags, script/nav/footer exclusion). |

Re-record the help-page fixture (needs network) with `python fixtures/help_fixtures.py`.

See [`TESTING.md`](TESTING.md) for six real adversarial conversations run live against the model, real fetches, and a real Jira issue.

## Known limitations

- **Retrieval is a crude, stdlib-only token-overlap score** against article *titles* (no embeddings), so a genuinely covered topic phrased very differently from its article's title can be missed. This is deliberately permissive rather than strict — `generate_grounded_answer` is the real, fails-closed gate on whether a match actually supports an answer, so a weak retrieval match is safe (it just gets a chance to be rejected by the drafting step) but a missed one still produces an avoidable content-gap redirect.
- **The redaction pattern pass over-redacts long numbers**: any 6–13 digit run is treated as a possible account/meter reference, so an ordinary large figure (e.g. an annual kWh total) gets redacted too. Chosen deliberately over the alternative of under-redacting a real reference number.
- `JIRA_SPRINT_ID` is a static numeric ID, resolved once; if the "Escalation - support agent" sprint is ever deleted and recreated, the ID needs re-resolving.
