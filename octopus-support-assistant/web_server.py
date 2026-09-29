"""Web chat backend for the Octopus Support Assistant.

A thin adapter over core.py, like cli.py: it calls core.handle_message() once per
/chat request and streams the result to a browser as Server-Sent Events. It holds
no classification/redaction/grounding logic. Unofficial demo; not affiliated with
Octopus Energy; no access to any real customer account.

This is a public, unauthenticated endpoint, so it is deliberately mean with money,
the same way tariff-advisor/web_server.py is: a per-IP burst rate limit and a
persisted daily token budget (both global and per-IP) that survives a process
restart. See the guardrail constants below. RateLimiter/TokenBudget/the session
store are duplicated-and-adapted from tariff-advisor/web_server.py rather than
imported -- these are two independently-deployable Render services with no shared
package between them.

core.handle_message() is a single opaque call (never .stream()-based) that makes
1-3 sequential model calls internally, so unlike tariff-advisor's web_server.py
there is no tool-use loop to drive here, and no token-by-token text to stream --
one "status" event while the call is in flight, then a single "text" event with
the whole reply, then "done". Real token usage is collected via core.py's
on_usage callback (invoked once per internal model call) and reconciled against
the persisted budget after each request.

Run locally:

    uvicorn web_server:app --reload

Needs Python 3.10+, `pip install -r requirements-web.txt`, and an
ANTHROPIC_API_KEY in the environment. core.py and cli.py do not need any of this.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import anthropic
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import core

log = logging.getLogger("octopus-support-assistant-web")

# ---------------------------------------------------------------------------
# Configuration (env-overridable; starting points, not calibrated numbers --
# tune against real on_usage-logged totals once this is deployed, the same
# empirical process tariff-advisor/web_server.py's own token-cap comment
# describes)
# ---------------------------------------------------------------------------

MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "1000"))
MAX_TURNS_PER_SESSION = int(os.environ.get("MAX_TURNS_PER_SESSION", "8"))  # lower than tariff-advisor's 12: costlier turns
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "500"))
PER_IP_RATE_LIMIT_PER_MIN = int(os.environ.get("PER_IP_RATE_LIMIT_PER_MIN", "5"))

# core.py's internal calls cap at 400 (classify) + 800 (answer) + 300 (redact) =
# 1500 max_tokens combined, but a *single message* can cost far more than that in
# *input* tokens: generate_grounded_answer's prompt embeds up to 3 full fetched
# article bodies. This is the pre-flight reserve for one whole handle_message()
# call, not a per-call cap (core.py's functions set their own max_tokens already).
MAX_TOKENS_RESERVE_PER_MESSAGE = int(os.environ.get("MAX_TOKENS_RESERVE_PER_MESSAGE", "12000"))
GLOBAL_DAILY_TOKEN_BUDGET = int(os.environ.get("GLOBAL_DAILY_TOKEN_BUDGET", "150000"))
PER_IP_DAILY_TOKEN_BUDGET = int(os.environ.get("PER_IP_DAILY_TOKEN_BUDGET", "30000"))
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:8000")
BUDGET_STATE_PATH = Path(os.environ.get("BUDGET_STATE_PATH", str(Path(__file__).with_name(".budget_state.json"))))

BUDGET_EXHAUSTED_MESSAGE = (
    "Today's demo budget for this assistant is used up, so it can't take new questions right "
    "now -- please try again tomorrow. In the meantime you can read the write-up or browse the code."
)

# ---------------------------------------------------------------------------
# Guardrails (duplicated-and-adapted from tariff-advisor/web_server.py -- see
# that file for the source of truth; port a bugfix there here too)
# ---------------------------------------------------------------------------


class RateLimiter:
    """A per-key sliding-window burst limit. Not persisted: it is a speed bump against bursts,
    not a cost control (that is TokenBudget's job)."""

    def __init__(self, limit_per_min: int):
        self._limit = limit_per_min
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True


class TokenBudget:
    """A daily token budget, global and per-key, persisted to a small JSON file.

    An in-memory-only counter would reopen the budget on every restart or redeploy, which
    defeats the point of a hard spend ceiling on a public endpoint. State is written after
    every recorded call and reloaded on construction, so a restart mid-day keeps the correct
    remaining balance. Resets when the UTC date rolls over.
    """

    def __init__(self, path: Path, global_limit: int, per_key_limit: int,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._path, self._global_limit, self._per_key_limit, self._clock = path, global_limit, per_key_limit, clock
        self._lock = threading.Lock()
        self._date = ""
        self._global_used = 0
        self._per_key_used: dict[str, int] = {}
        self._load()

    def _today(self) -> str:
        return self._clock().date().isoformat()

    def _load(self) -> None:
        data: dict[str, Any] = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        today = self._today()
        if data.get("date") == today:
            self._date = today
            self._global_used = int(data.get("global_used", 0))
            self._per_key_used = {str(k): int(v) for k, v in data.get("per_key_used", {}).items()}
        else:
            self._date, self._global_used, self._per_key_used = today, 0, {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps({"date": self._date, "global_used": self._global_used, "per_key_used": self._per_key_used}))
        except OSError:
            log.warning("Could not persist token budget state to %s", self._path)

    def _roll_if_new_day(self) -> None:
        today = self._today()
        if today != self._date:
            self._date, self._global_used, self._per_key_used = today, 0, {}
            self._save()

    def remaining(self, key: str) -> tuple[int, int]:
        """(global_remaining, key_remaining), rolling over to a new day first if needed."""
        with self._lock:
            self._roll_if_new_day()
            return self._global_limit - self._global_used, self._per_key_limit - self._per_key_used.get(key, 0)

    def can_afford(self, key: str, estimate: int) -> bool:
        global_remaining, key_remaining = self.remaining(key)
        return global_remaining >= estimate and key_remaining >= estimate

    def record(self, key: str, tokens: int) -> None:
        with self._lock:
            self._roll_if_new_day()
            self._global_used += tokens
            self._per_key_used[key] = self._per_key_used.get(key, 0) + tokens
            self._save()


rate_limiter = RateLimiter(PER_IP_RATE_LIMIT_PER_MIN)
token_budget = TokenBudget(BUDGET_STATE_PATH, GLOBAL_DAILY_TOKEN_BUDGET, PER_IP_DAILY_TOKEN_BUDGET)
category_cache = core.CategoryIndexCache()  # persists across requests -- avoids refetching all CATEGORY_SLUGS every message

# ---------------------------------------------------------------------------
# Session store (in-memory, demo-scale: lost on restart, not multi-instance safe)
# ---------------------------------------------------------------------------

sessions: "OrderedDict[str, list[dict[str, str]]]" = OrderedDict()
sessions_lock = threading.Lock()


def get_session(session_id: Optional[str]) -> tuple[str, list[dict[str, str]]]:
    with sessions_lock:
        if session_id and session_id in sessions:
            sessions.move_to_end(session_id)
            return session_id, sessions[session_id]
        new_id = session_id or str(uuid.uuid4())
        sessions[new_id] = []
        sessions.move_to_end(new_id)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
        return new_id, sessions[new_id]


# Lets a client (the "New conversation" button) tell the backend an in-flight
# session has been abandoned. handle_message() has no internal yield points
# (unlike tariff-advisor's agentic tool loop), so this cannot interrupt a turn
# already in flight -- its tokens are already spent by the time cancellation is
# noticed. Kept anyway for API/UX symmetry with the reset button, and so a
# *future* turn on an abandoned session id doesn't proceed. Bounded the same way
# as `sessions`.
cancelled_sessions: "OrderedDict[str, None]" = OrderedDict()
cancelled_sessions_lock = threading.Lock()


def mark_cancelled(session_id: str) -> None:
    with cancelled_sessions_lock:
        cancelled_sessions[session_id] = None
        cancelled_sessions.move_to_end(session_id)
        while len(cancelled_sessions) > MAX_SESSIONS:
            cancelled_sessions.popitem(last=False)


def consume_cancelled(session_id: str) -> bool:
    """True and clears the flag if this session was marked cancelled; False otherwise."""
    with cancelled_sessions_lock:
        if session_id in cancelled_sessions:
            del cancelled_sessions[session_id]
            return True
        return False


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Octopus Support Assistant -- web chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

anthropic_client: Any = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment; swappable in tests
http_get: Callable[[str], str] = core._http_get  # swappable in tests, same pattern as anthropic_client/category_cache


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class CancelRequest(BaseModel):
    session_id: str


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def render_reply_text(result: dict[str, Any]) -> str:
    """Mirrors cli.py's render(): a GROUNDED_ANSWER gets its citation URLs
    appended as 'Source: <url>' lines; a REDIRECT is sent as-is."""
    if result["type"] != "GROUNDED_ANSWER":
        return result["text"]
    lines = [result["text"], ""]
    lines.extend(f"Source: {url}" for url in result["citations"])
    return "\n".join(lines)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/cancel")
def cancel(req: CancelRequest) -> dict[str, str]:
    """Tells the backend a session has been abandoned, so a *future* turn on it
    doesn't proceed. See the cancelled_sessions comment above for why this
    can't interrupt a turn already in flight."""
    mark_cancelled(req.session_id)
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"

    if not req.message.strip():
        return JSONResponse(status_code=400, content={"error": "Message cannot be empty."})
    if len(req.message) > MAX_INPUT_CHARS:
        return JSONResponse(status_code=400, content={"error": f"Message too long (max {MAX_INPUT_CHARS} characters)."})
    if not rate_limiter.allow(ip):
        return JSONResponse(status_code=429, content={"error": "Too many messages -- please slow down and try again in a minute."})

    session_id, history = get_session(req.session_id)
    if len(history) >= MAX_TURNS_PER_SESSION * 2:
        return JSONResponse(status_code=400, content={"error": "This conversation has reached its length limit -- please start a new one."})
    if not token_budget.can_afford(ip, MAX_TOKENS_RESERVE_PER_MESSAGE):
        return JSONResponse(status_code=429, content={"error": BUDGET_EXHAUSTED_MESSAGE})

    def stream() -> Iterator[str]:
        try:
            if consume_cancelled(session_id):
                # A /cancel for this session raced ahead of this /chat request (e.g. a fast
                # reset click just after sending) -- skip the expensive call entirely rather
                # than spend tokens on a turn the client has already abandoned.
                return
            yield sse("status", "Thinking…")

            usage_totals = [0, 0]  # [input, output] -- mutable cell for the on_usage closure

            def on_usage(call_name: str, input_tokens: int, output_tokens: int) -> None:
                usage_totals[0] += input_tokens
                usage_totals[1] += output_tokens

            result = core.handle_message(
                req.message, history, client=anthropic_client,
                http_get=http_get, index_fetch=category_cache, on_usage=on_usage,
            )
            token_budget.record(ip, usage_totals[0] + usage_totals[1])

            history.append({"role": "user", "content": req.message})
            history.append({"role": "assistant", "content": result["text"]})

            yield sse("text", render_reply_text(result))
        except anthropic.RateLimitError as exc:
            # Both genuine request-rate limiting and an account/workspace credit or spend
            # limit being exhausted raise this -- indistinguishable from here, but both mean
            # the same thing to a visitor: no budget available right now. Same message as
            # the app's own daily cap, rather than the generic catch-all below, which would
            # wrongly read as a transient glitch worth retrying immediately.
            log.warning("Anthropic rate/quota limit hit: %s", exc)
            yield sse("text", "\n\n" + BUDGET_EXHAUSTED_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - a public endpoint must never crash the stream, whatever the cause
            log.warning("Chat turn failed: %s: %s", type(exc).__name__, exc)
            yield sse("text", "\n\nSorry, something went wrong. Please try again.")
        finally:
            yield sse("done", {"session_id": session_id})

    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    import uvicorn
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
