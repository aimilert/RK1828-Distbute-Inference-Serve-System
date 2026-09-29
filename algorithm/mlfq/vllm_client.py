"""轻量异步 vLLM 流式客户端(OpenAI 兼容 /v1/chat/completions)。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


class VLLMError(RuntimeError):
    pass


@dataclass
class StreamResult:
    request_id: str = ""
    status: int = 0
    output_tokens: int = 0
    first_token_time: float | None = None
    request_end_time: float | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class VLLMClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        model: str = "Qwen3-8B",
        timeout_seconds: float = 1200.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def check_connection(self) -> None:
        url = f"{self.base_url}/v1/models"
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise VLLMError(f"/v1/models -> HTTP {resp.status}")
        except VLLMError:
            raise
        except Exception as exc:
            raise VLLMError(f"Cannot connect to vLLM at {self.base_url}: {exc}") from exc

    async def get_model_info(self) -> dict[str, Any]:
        url = f"{self.base_url}/v1/models"
        session = await self._get_session()
        async with session.get(url) as resp:
            if resp.status != 200:
                raise VLLMError(f"GET /v1/models -> HTTP {resp.status}")
            payload = await resp.json()
        data = payload.get("data", []) if isinstance(payload, dict) else []
        for model in data:
            if isinstance(model, dict):
                return {"id": model.get("id"), "max_model_len": model.get("max_model_len")}
        return {"id": None, "max_model_len": None}

    async def get_metrics(self) -> str:
        """返回 Prometheus /metrics 文本(失败返回空串)。"""
        url = f"{self.base_url}/metrics"
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""
                return await resp.text()
        except Exception:
            return ""

    async def stream_chat(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        request_start_time: float,
    ) -> StreamResult:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        result = StreamResult()
        session = await self._get_session()
        url = f"{self.base_url}/v1/chat/completions"
        first_token_time: float | None = None
        delta_count = 0
        usage_tokens: int | None = None

        try:
            async with session.post(url, json=payload) as resp:
                result.status = resp.status
                if resp.status != 200:
                    body = await resp.text()
                    result.error = f"HTTP {resp.status}: {body[:300]}"
                    result.request_end_time = time.monotonic()
                    return result
                async for raw in resp.content:
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict):
                        usage = chunk.get("usage")
                        if isinstance(usage, dict):
                            usage_tokens = usage.get("completion_tokens")
                        choices = chunk.get("choices")
                        if isinstance(choices, list) and choices:
                            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
                            if isinstance(delta, dict) and delta.get("content"):
                                if first_token_time is None:
                                    first_token_time = time.monotonic()
                                delta_count += 1
                result.request_end_time = time.monotonic()
        except asyncio.TimeoutError:
            result.error = f"timeout after {self.timeout_seconds}s"
            result.request_end_time = time.monotonic()
        except aiohttp.ClientError as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.request_end_time = time.monotonic()

        result.output_tokens = usage_tokens if usage_tokens is not None else delta_count
        result.first_token_time = first_token_time
        result.extra["prompt_chars"] = len(prompt)
        return result
