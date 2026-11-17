"""A fake OpenAI-compatible endpoint for the real ``openai`` SDK (via ``httpx2.MockTransport``)."""

import json
from typing import Any, Callable

import httpx2

API_KEY_SENTINEL = "sk-or-SENTINEL-llm-key-0123456789"
BODY_SENTINEL = "SENTINEL-provider-text-4242"


def completion(content: str | None, finish_reason: str = "stop", usage: dict | None = None) -> dict:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def movies_json(n: int, start: int = 0) -> str:
    return json.dumps(
        {
            "movies": [
                {"title": f"Title {i}", "year": 1990 + i % 30, "category": "alpha"} for i in range(start, start + n)
            ]
        }
    )


def error(status: int, message: str) -> tuple[int, dict]:
    return status, {"error": {"message": f"{message} {BODY_SENTINEL}", "code": status}}


class FakeLLM:
    """Replies come from a list (status, json) or callables; every request is recorded."""

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append({"body": body, "headers": dict(request.headers), "url": str(request.url)})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Callable):
            reply = reply(body)
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply if isinstance(reply, tuple) else (200, reply)
        return httpx2.Response(status, json=payload)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def modes(self) -> list[str]:
        out = []
        for r in self.requests:
            rf = r["body"].get("response_format")
            out.append(rf["type"] if rf else "text")
        return out
