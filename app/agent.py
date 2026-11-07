"""The chat loop: Claude answers HSE questions by calling the incident tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import anthropic
from pymongo.collection import Collection

from . import config
from .tools import run_tool, tool_definitions

MAX_TOOL_ROUNDS = 12
MAX_JSON_RETRIES = 2
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are an HSE (health, safety and environment) assistant for safety professionals. You answer \
questions using a database of 4,847 OSHA serious-injury and fatality reports dated July 2015 to \
August 2017 (2,964 fatal, 1,883 nonfatal). Each record has a description, the full abstract \
narrative, OSHA keyword tags, and coded fields: degree of injury, event type, nature of injury, \
part of body, environmental factor, human factor, whether the task was regularly assigned, and \
for construction sites the end use, project type, project cost and building stories.

How to work:
- Base every factual claim about incidents on tool results. Use incident_statistics for counts, \
rankings and trends, and search_incidents / get_incident for examples. Cite incident ids and \
dates when you describe specific cases.
- If a question doesn't map onto a filter value exactly, pick the closest values, or use the \
`text` / `keyword` filters, and say which you used.
- "Other" is a common coded value for the factor fields; mention it when it dominates a result \
instead of presenting it as a finding.
- The data is a sample of reports OSHA chose to publish, not a full census of US workplace \
injuries. Don't present counts as national rates.
- When asked for prevention advice, ground it in the patterns in the data and in widely \
accepted controls (hierarchy of controls, OSHA standards), and keep data and general guidance \
clearly separated.
- Be concise. Use short markdown tables for statistics.
"""


@dataclass
class Conversation:
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HSEAgent:
    def __init__(self, coll: Collection, values: dict[str, list[str]], client: Any | None = None):
        self.coll = coll
        self.values = values
        self.tools = tool_definitions(values)
        self.client = client or anthropic.AsyncAnthropic()
        self.conversations: dict[str, Conversation] = {}

    def _request_params(self, messages: list) -> dict:
        return dict(
            model=config.CLAUDE_MODEL,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": config.CLAUDE_EFFORT},
            cache_control={"type": "ephemeral"},
            # On a safety-classifier decline the API re-runs the request on a fallback model.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    async def chat(self, conversation_id: str, user_text: str) -> AsyncIterator[dict]:
        """Yield events: text, tool, error, done."""
        convo = self.conversations.setdefault(conversation_id, Conversation())
        async with convo.lock:
            checkpoint = len(convo.messages)
            convo.messages.append({"role": "user", "content": user_text})
            ok = False
            try:
                async for event in self._run(convo.messages):
                    if event["type"] == "done":
                        ok = True
                    yield event
            except anthropic.APIError as exc:
                yield {"type": "error", "message": f"Claude API error: {getattr(exc, 'message', str(exc))}"}
            finally:
                if not ok:
                    # Leave the history as it was before this turn so the next request is valid.
                    del convo.messages[checkpoint:]

    async def _run(self, messages: list) -> AsyncIterator[dict]:
        json_retries = 0
        rounds = 0
        while rounds < MAX_TOOL_ROUNDS:
            try:
                async with self.client.beta.messages.stream(**self._request_params(messages)) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield {"type": "text", "text": event.text}
                    response = await stream.get_final_message()
            except ValueError:
                # Tool input that the SDK couldn't parse as JSON; ask again.
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    yield {"type": "error", "message": "Claude sent malformed tool input repeatedly."}
                    return
                continue
            json_retries = 0

            if response.stop_reason == "refusal":
                yield {"type": "error", "message": "The request was declined. Try rephrasing the question."}
                return

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "max_tokens":
                if tool_uses:
                    yield {"type": "error", "message": "The response was cut off before a tool call finished."}
                    return
                yield {"type": "done"}
                return
            if not tool_uses:
                yield {"type": "done"}
                return

            results = []
            for block in tool_uses:
                yield {"type": "tool", "name": block.name, "input": block.input}
                content, is_error = await asyncio.to_thread(
                    run_tool, self.coll, self.values, block.name, block.input
                )
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}
                )
            # All results for one assistant turn go back in a single user message.
            messages.append({"role": "user", "content": results})
            rounds += 1

        yield {"type": "error", "message": f"Stopped after {MAX_TOOL_ROUNDS} rounds of tool calls."}


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
