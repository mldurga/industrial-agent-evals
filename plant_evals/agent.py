"""Run one question through the tool loop and return a trace."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from plant_evals.fake_model import FakeClient
from plant_evals.store import PlantStore, find_root, format_time
from plant_evals.tools import Toolset, tool_schemas

MAX_ROUNDS = 6

SYSTEM_PROMPT = """You answer questions about one plant, the Riverside cooling-water system, using only the tools.
History covers 2026-03-01T00:00:00Z inclusive through 2026-03-04T00:00:00Z exclusive. Samples are every 5 minutes.
Equipment: P-101A and P-101B pumps, E-201 heat exchanger, TK-301 basin, and the cooling-water header.

Rules:
- Call a tool before you state a tag, a number, a time, or an alarm. Do not invent tags or values.
- Send times as UTC, start inclusive and end exclusive, formatted 2026-03-02T06:00:00Z.
- If tags, history, alarms, and event notes cannot answer the question, say "I don't know" and name what is missing.
- Do not guess shift roles, procedures, setpoint history, vendor data, or anything in the future.
- If the tags only partly support a cause, say what they show and say what you do not know.
- For a unit conversion, start from the tool value and state the factor.
- Lead with the finding. Keep the answer short.
"""


def run_question(
    question: str,
    *,
    store: PlantStore | None = None,
    fake: bool = False,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    question_id: str = "",
    category: str = "",
) -> dict:
    plant = store or PlantStore.load(find_root() / "data")
    tools = Toolset(plant)
    model_name = "fake" if fake else (model or os.environ.get("OPENAI_MODEL") or "")
    resolved_base = base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
    if fake:
        client = FakeClient(plant)
    else:
        if not model_name:
            raise RuntimeError("Set OPENAI_MODEL or pass --fake.")
        if not resolved_key:
            raise RuntimeError("Set OPENAI_API_KEY or pass --fake.")
        client = OpenAIClient(base_url=resolved_base, api_key=resolved_key, model=model_name)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_records: list[dict] = []
    started_at = datetime.now(timezone.utc)
    clock = time.perf_counter()
    error = None
    try:
        for _ in range(MAX_ROUNDS):
            reply = client.complete(messages, tool_schemas())
            messages.append(_assistant_message(reply))
            calls = reply.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                tool_clock = time.perf_counter()
                result = tools.call(call["name"], call.get("arguments") or {})
                tool_records.append(
                    {
                        "id": call.get("id"),
                        "name": call["name"],
                        "arguments": call.get("arguments") or {},
                        "result": result,
                        "duration_ms": round((time.perf_counter() - tool_clock) * 1000, 2),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "name": call["name"],
                        "content": result,
                    }
                )
        else:
            messages.append(
                {
                    "role": "user",
                    "content": "Stop calling tools. Answer from the results you have.",
                }
            )
            reply = client.complete(messages, None)
            messages.append(_assistant_message(reply))
    except Exception as exc:  # recorded on the trace; collect continues
        error = f"{type(exc).__name__}: {exc}"

    final = ""
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            final = str(message["content"])
            break
    return {
        "question_id": question_id,
        "category": category,
        "question": question,
        "model": model_name or "unset",
        "started_at": format_time(started_at),
        "duration_ms": round((time.perf_counter() - clock) * 1000, 2),
        "messages": messages,
        "tool_calls": tool_records,
        "final_answer": final,
        "error": error,
    }


def _assistant_message(reply: dict) -> dict:
    message = {
        "role": "assistant",
        "content": reply.get("content"),
        "tool_calls": reply.get("tool_calls") or [],
    }
    return message


class OpenAIClient:
    """Chat completions against any OpenAI-compatible base URL."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, messages: list[dict], tools: list[dict] | None) -> dict:
        url = self.base_url
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        body: dict = {
            "model": self.model,
            "temperature": 0,
            "messages": [_to_api(message) for message in messages],
        }
        if tools:
            body["tools"] = tools
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        choice = payload["choices"][0]["message"]
        tool_calls = []
        for call in choice.get("tool_calls") or []:
            raw = call.get("function", {}).get("arguments") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                arguments = {"_raw": raw}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{len(tool_calls) + 1}",
                    "name": call["function"]["name"],
                    "arguments": arguments,
                }
            )
        return {
            "role": "assistant",
            "content": choice.get("content"),
            "tool_calls": tool_calls,
        }


def _to_api(message: dict) -> dict:
    role = message["role"]
    if role == "tool":
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        return {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id"),
            "content": content,
        }
    if role == "assistant" and message.get("tool_calls"):
        api_calls = []
        for call in message["tool_calls"]:
            api_calls.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
            )
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": api_calls,
        }
    return {"role": role, "content": message.get("content") or ""}
