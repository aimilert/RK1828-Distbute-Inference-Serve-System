"""上层封装调度器。

调度语义:
    - 每个 session 是一条严格串行的 round 链(下一轮须等上一轮完成 + 工具等待结束)。
    - 全局固定 K 个并发槽位(对应 vLLM continuous batching 可容纳的请求数)。
    - 每当有槽位空闲, 在 ready 集合中按策略权重打分, 选择得分最高者派发(跨会话补位)。
    - round 完成后, 若还有下一轮, 模拟工具延迟(sleep tool_latency_ms)后进入 ready。
    - MLFQ 额外启用"长任务分流": prompt 超阈值的 round 单独限流, 防止 prefill 突发挤占 decode。

指标: Makespan / 吞吐 / E2E/TTFT/TPOT 分位数 / Bubble 比 / 前缀复用 / 工具等待。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from config import (
    CHAT_TEMPLATE_MARGIN,
    LONG_PROMPT_MAX_CONCURRENT,
    LONG_PROMPT_THRESHOLD,
    MAX_MODEL_LEN,
    TOOL_WAIT_SCALE,
)
from factors import FactorGraph
from vllm_client import VLLMClient
from workload import AgentRound, AgentSession

_CODE_UNIT = (
    "def retry_with_backoff(fn, retries=3, base=0.5):\n"
    "    for attempt in range(retries):\n"
    "        try:\n"
    "            return fn()\n"
    "        except TransientError:\n"
    "            time.sleep(base * (2 ** attempt))\n"
    "    raise FinalError('gave up')\n"
    "# user task: fix the failing integration test in the payments module\n"
)
_NEW_UNIT = (
    "# implement a scheduler that overlaps tool latency with other sessions\n"
    "# 1. maintain per-session serial chains\n"
    "# 2. keep the decode pipeline full across sessions\n"
    "# 3. co-locate prefix-sharing rounds\n"
)


class PromptCache:
    """生成带共享前缀的 prompt, 用于触发 vLLM prefix cache。

    同一 session 的相邻 round 共享 base_pool 的前 prefix_len 个 token,
    从而让 vLLM(--enable-prefix-caching)命中 KV 缓存; 不同 session 用
    不同的 base 偏移, 保证跨 session 前缀不同(贴近真实语义)。
    """

    def __init__(self, tokenizer):
        self._tokenizer = tokenizer
        self._cache: dict[tuple, str] = {}
        self._base_pool: list[int] | None = None
        self._new_pool: list[int] | None = None
        if tokenizer is not None:
            try:
                self._base_pool = self._gen_pool(_CODE_UNIT, 40000)
                self._new_pool = self._gen_pool(_NEW_UNIT, 8000)
            except Exception:
                self._tokenizer = None
                self._base_pool = None
                self._new_pool = None

    def _gen_pool(self, unit: str, target: int) -> list[int]:
        ids = self._tokenizer.encode(unit, add_special_tokens=False)
        if not ids:
            ids = [self._tokenizer.eos_token_id]
        reps = (target + len(ids) - 1) // len(ids)
        return (ids * reps)[:target]

    @staticmethod
    def _session_offset(session_id: str) -> int:
        h = hashlib.md5(session_id.encode("utf-8")).hexdigest()
        return int(h[:6], 16) % 2000

    def get(self, session_id: str, prefix_len: int, prompt_len: int) -> str:
        if self._tokenizer is None or self._base_pool is None:
            return "tok " * max(1, prompt_len)
        key = (session_id, prefix_len, prompt_len)
        if key not in self._cache:
            offset = self._session_offset(session_id)
            base = self._base_pool[offset : offset + prefix_len]
            new_n = max(1, prompt_len - prefix_len)
            new = self._new_pool[:new_n]
            ids = base + new
            try:
                text = self._tokenizer.decode(ids)
            except Exception:
                text = " ".join(str(i) for i in ids)
            self._cache[key] = text
        return self._cache[key]


@dataclass
class RequestMetrics:
    request_id: str
    session_id: str
    round_id: int
    prompt_len: int
    output_len: int
    prefix_len: int
    tool_latency_ms: float
    queuing: float
    start: float
    first_token: float | None
    end: float
    output_tokens: int
    ok: bool
    error: str = ""

    @property
    def ttft(self) -> float | None:
        if self.first_token is None:
            return None
        return self.first_token - self.start

    @property
    def e2e(self) -> float:
        return self.end - self.start

    @property
    def tpot(self) -> float | None:
        if self.first_token is None or self.output_tokens <= 1:
            return None
        return (self.end - self.first_token) / max(self.output_tokens - 1, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "round_id": self.round_id,
            "prompt_len": self.prompt_len,
            "output_len": self.output_len,
            "prefix_len": self.prefix_len,
            "tool_latency_ms": round(self.tool_latency_ms, 3),
            "queuing": round(self.queuing, 6),
            "start": round(self.start, 6),
            "first_token": round(self.first_token, 6) if self.first_token else None,
            "end": round(self.end, 6),
            "ttft": round(self.ttft, 6) if self.ttft is not None else None,
            "e2e": round(self.e2e, 6),
            "tpot": round(self.tpot, 6) if self.tpot is not None else None,
            "output_tokens": self.output_tokens,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class PolicyResult:
    policy: str = ""
    weights: tuple[float, float, float, float, float] = ()
    metrics: list[RequestMetrics] = field(default_factory=list)
    makespan: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"policy": self.policy, "weights": list(self.weights), "summary": self.summary}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "mean": 0.0, "max": 0.0, "count": 0}
    return {
        "p50": _pct(values, 0.5),
        "p90": _pct(values, 0.9),
        "p99": _pct(values, 0.99),
        "mean": sum(values) / len(values),
        "max": max(values),
        "count": len(values),
    }


def compute_bubble(metrics: list[RequestMetrics], makespan: float, concurrency: int) -> dict[str, Any]:
    ok = [m for m in metrics if m.ok]
    slot_time = sum(max(0.0, m.end - m.start) for m in ok)
    available = float(concurrency) * makespan if makespan > 0 else 0.0
    idle = max(0.0, available - slot_time)
    bubble_ratio = idle / available if available > 0 else 0.0
    return {
        "bubble_ratio": round(bubble_ratio, 6),
        "slot_idle_seconds": round(idle, 4),
        "slot_time_consumed": round(slot_time, 4),
    }


def build_summary(
    metrics: list[RequestMetrics],
    makespan: float,
    concurrency: int,
    tool_wait_seconds: float,
) -> dict[str, Any]:
    ok = [m for m in metrics if m.ok]
    total_output = sum(m.output_tokens for m in ok)
    total_prompt = sum(m.prompt_len for m in ok)
    total_prefix = sum(m.prefix_len for m in ok)
    wall = makespan if makespan > 0 else 1.0
    e2e = [m.e2e for m in ok]
    ttft = [m.ttft for m in ok if m.ttft is not None]
    tpot = [m.tpot for m in ok if m.tpot is not None]
    queuing = [m.queuing for m in ok]
    return {
        "total_requests": len(metrics),
        "ok": len(ok),
        "fail": len(metrics) - len(ok),
        "makespan": round(makespan, 4),
        "throughput_req_s": round(len(ok) / wall, 4),
        "output_tokens_s": round(total_output / wall, 4),
        "tool_wait_seconds": round(tool_wait_seconds, 4),
        "prefix_reuse_ratio": round(total_prefix / total_prompt, 4) if total_prompt else 0.0,
        "e2e": _stats(e2e),
        "ttft": _stats(ttft),
        "tpot": _stats(tpot),
        "queuing": _stats(queuing),
        "bubble": compute_bubble(metrics, makespan, concurrency),
    }


async def run_policy(
    sessions: list[AgentSession],
    weights: tuple[float, float, float, float, float],
    *,
    client: VLLMClient,
    tokenizer,
    concurrency: int,
    prompt_cache: PromptCache,
    temperature: float = 0.0,
    max_model_len: int | None = None,
    enable_long_split: bool = False,
) -> PolicyResult:
    graph = FactorGraph.build(sessions)
    sessions_by_id = {s.session_id: s for s in sessions}
    t0 = time.monotonic()

    state: dict[str, dict[str, Any]] = {
        s.session_id: {"next": 0, "phase": "ready", "ready_time": t0}
        for s in sessions
    }

    metrics: list[RequestMetrics] = []
    in_flight: dict[asyncio.Task, tuple[AgentRound, float]] = {}
    tool_tasks: dict[asyncio.Task, str] = {}
    long_inflight = 0
    tool_wait_seconds = 0.0
    ml = max_model_len or MAX_MODEL_LEN

    def ready_rounds() -> list[tuple[AgentRound, float]]:
        out: list[tuple[AgentRound, float]] = []
        for s in sessions:
            st = state[s.session_id]
            if st["phase"] == "ready":
                r = s.rounds[st["next"]]
                out.append((r, st["ready_time"]))
        return out

    async def execute(r: AgentRound, ready_time: float) -> None:
        # 裁剪 prompt 到上下文窗口(留输出 + 聊天模板余量)
        budget = ml - r.output_len - CHAT_TEMPLATE_MARGIN
        if budget < 1:
            budget = 1
        sent_prompt = max(1, min(r.prompt_len, budget))
        sent_prefix = max(0, min(r.prefix_len, sent_prompt - 1))
        prompt = prompt_cache.get(r.session_id, sent_prefix, sent_prompt)
        start = time.monotonic()
        result = await client.stream_chat(
            prompt,
            max_tokens=r.output_len,
            temperature=temperature,
            request_start_time=start,
        )
        end = result.request_end_time or time.monotonic()
        first = result.first_token_time
        metrics.append(
            RequestMetrics(
                request_id=r.request_id,
                session_id=r.session_id,
                round_id=r.round_id,
                prompt_len=sent_prompt,
                output_len=r.output_len,
                prefix_len=sent_prefix,
                tool_latency_ms=r.tool_latency_ms,
                queuing=max(0.0, start - ready_time),
                start=start,
                first_token=first,
                end=end,
                output_tokens=result.output_tokens,
                ok=(result.error is None and result.status in (0, 200)),
                error=result.error or "",
            )
        )

    def select_best(
        rr: list[tuple[AgentRound, float]],
    ) -> tuple[AgentRound, float] | None:
        candidates = list(rr)
        if enable_long_split:
            allowed = [
                x for x in candidates
                if not (x[0].prompt_len > LONG_PROMPT_THRESHOLD and long_inflight >= LONG_PROMPT_MAX_CONCURRENT)
            ]
            if allowed:
                candidates = allowed
            else:
                return None  # 全被长任务限流, 等待长任务完成
        if all(w == 0.0 for w in weights):
            candidates.sort(key=lambda x: (x[1], x[0].request_id))
            return candidates[0]
        ready_ids = [x[0].request_id for x in candidates]
        ready_time_map = {x[0].request_id: x[1] for x in candidates}
        now = time.monotonic()
        best = max(
            candidates,
            key=lambda x: graph.score(x[0].request_id, weights, now, ready_time_map, ready_ids),
        )
        return best

    def dispatch_one() -> bool:
        nonlocal long_inflight
        rr = ready_rounds()
        if not rr:
            return False
        chosen = select_best(rr)
        if chosen is None:
            return False
        r, ready_time = chosen
        state[r.session_id]["phase"] = "running"
        task = asyncio.ensure_future(execute(r, ready_time))
        in_flight[task] = (r, ready_time)
        if r.prompt_len > LONG_PROMPT_THRESHOLD:
            long_inflight += 1
        return True

    async def wait_tool(session_id: str, seconds: float) -> None:
        nonlocal tool_wait_seconds
        scaled_seconds = seconds * TOOL_WAIT_SCALE
        await asyncio.sleep(scaled_seconds)
        tool_wait_seconds += scaled_seconds

    # 初始派发
    while len(in_flight) < concurrency:
        if not dispatch_one():
            break

    while in_flight or tool_tasks:
        wait_set = set(in_flight.keys()) | set(tool_tasks.keys())
        done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task in in_flight:
                r, _ready = in_flight.pop(task)
                if r.prompt_len > LONG_PROMPT_THRESHOLD:
                    long_inflight -= 1
                st = state[r.session_id]
                st["next"] += 1
                s = sessions_by_id[r.session_id]
                if st["next"] < len(s.rounds):
                    nxt = s.rounds[st["next"]]
                    st["phase"] = "waiting_tool"
                    tw = nxt.tool_latency_ms / 1000.0
                    if tw > 0:
                        ttask = asyncio.ensure_future(wait_tool(r.session_id, tw))
                        tool_tasks[ttask] = r.session_id
                    else:
                        st["phase"] = "ready"
                        st["ready_time"] = time.monotonic()
                else:
                    st["phase"] = "done"
            else:
                sid = tool_tasks.pop(task)
                st = state[sid]
                st["phase"] = "ready"
                st["ready_time"] = time.monotonic()

        while len(in_flight) < concurrency:
            if not dispatch_one():
                break

    makespan = (time.monotonic() - t0) if metrics else 0.0
    summary = build_summary(metrics, makespan, concurrency, tool_wait_seconds)
    return PolicyResult(weights=weights, metrics=metrics, makespan=makespan, summary=summary)
