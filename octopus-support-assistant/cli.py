"""Command-line interface for the Octopus Support Assistant.

A thin wrapper: print the required opening disclosure, then read messages
and print whatever core.handle_message() returns. All classification,
redaction, grounding and Jira logic live in core.py. This is the only file
that touches stdin/stdout, imports no client library, and reads no
environment variables itself -- core.py owns all of that (see
ArchitectureTests in test_core.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import core

DISCLOSURE = (
    "Octopus Support Assistant (unofficial demo)\n"
    "I'm an independent AI assistant -- not affiliated with, endorsed by, or "
    "operated by Octopus Energy.\n"
    "I have no access to any real Octopus account, so please don't share "
    "personal or account details; nothing typed here is stored beyond this "
    "session.\n"
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Chat with the Octopus Support Assistant. Informational demo only; not affiliated with Octopus Energy.",
    )
    p.add_argument("--once", metavar="MESSAGE", help="send a single message, print the response, and exit (for scripted transcripts)")
    p.add_argument("--json", action="store_true", help="print the raw response as JSON instead of rendered text")
    return p


def render(response: dict[str, Any]) -> str:
    if response["type"] == "GROUNDED_ANSWER":
        lines = [response["text"], ""]
        lines.extend(f"Source: {url}" for url in response["citations"])
        return "\n".join(lines)
    return response["text"]  # REDIRECT


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)

    print(DISCLOSURE)
    history: list[dict[str, str]] = []

    def send(message: str) -> None:
        response = core.handle_message(message, history, client=None)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response["text"]})
        print(json.dumps(response, indent=2) if args.json else render(response))

    if args.once is not None:
        send(args.once)
        return 0

    print("Type 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            message = input("You: ").strip()
        except EOFError:
            print()
            return 0
        if not message:
            continue
        if message.lower() in ("exit", "quit"):
            return 0
        send(message)
        print()


if __name__ == "__main__":
    sys.exit(main())
