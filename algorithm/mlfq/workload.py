"""从 TraceLab 真实 agent trace 构建多会话串行链工作负载。

数据源: TraceLab-MetaX/my_real_workload.jsonl
  - 真实 19 round / 2 sessions(Claude 10 轮 + Codex 9 轮)
  - 字段: session_id, round_id, prompt_len, output_len, prefix_len, tool_latency_ms

每 session 是一条严格串行的 round 链(下一轮必须等上一轮完成 + 工具等待结束)。
调度自由度来自: ① 跨会话选择派发哪个 ready round; ② 工具等待期由其他 session 补位。

当 num_sessions 大于真实 session 数时, 从真实 session 模板 bootstrap 合成
(对 prompt/output/prefix/tool_latency 加 ±jitter 扰动, 保留串行链与工具长尾结构)。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from config import BOOTSTRAP_SEED, JITTER, TRACE_WORKLOAD


@dataclass
class AgentRound:
    request_id: str
    session_id: str
    round_id: int
    prompt_len: int
    output_len: int
    prefix_len: int
    tool_latency_ms: float
    arrival_time: float = 0.0


@dataclass
class AgentSession:
    session_id: str
    rounds: list[AgentRound] = field(default_factory=list)


def _read_real_sessions(path: Path) -> list[AgentSession]:
    by_session: dict[str, list[dict]] = {}
    order: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            sid = rec.get("session_id") or rec.get("request_id")
            if sid not in by_session:
                by_session[sid] = []
                order[sid] = idx
            by_session[sid].append(rec)

    sessions: list[AgentSession] = []
    for sid, recs in by_session.items():
        recs.sort(key=lambda r: (r.get("round_id", 0), order[sid]))
        rounds: list[AgentRound] = []
        for i, r in enumerate(recs):
            prompt = int(r.get("prompt_len", 0))
            output = int(r.get("output_len", 0))
            prefix = int(r.get("prefix_len", 0))
            rounds.append(
                AgentRound(
                    request_id=r.get("request_id") or f"{sid}_round_{i}",
                    session_id=sid,
                    round_id=int(r.get("round_id", i)),
                    prompt_len=prompt,
                    output_len=output,
                    prefix_len=prefix,
                    tool_latency_ms=float(r.get("tool_latency_ms", 0.0) or 0.0),
                    arrival_time=float(r.get("arrival_time", 0.0) or 0.0),
                )
            )
        rounds = [r for r in rounds if r.prompt_len > 0 and r.output_len > 0]
        if rounds:
            sessions.append(AgentSession(session_id=sid, rounds=rounds))

    sessions.sort(key=lambda s: order[s.session_id])
    return sessions


def _jitter(value: float, rng: random.Random, lo: float, hi: float) -> float:
    value = value * (1.0 + rng.uniform(-JITTER, JITTER))
    return max(lo, min(hi, value))


def build_workload(
    num_sessions: int,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[list[AgentSession], dict]:
    real = _read_real_sessions(TRACE_WORKLOAD)
    if not real:
        raise RuntimeError(f"无法从真实 trace 解析出会话: {TRACE_WORKLOAD}")

    if num_sessions <= len(real):
        sessions = real[:num_sessions]
        mode = "real"
    else:
        rng = random.Random(seed)
        sessions: list[AgentSession] = []
        for i in range(num_sessions):
            tpl = real[i % len(real)]
            sid = f"boot_{i:03d}_{tpl.session_id}"
            rounds: list[AgentRound] = []
            for j, r in enumerate(tpl.rounds):
                prompt = int(_jitter(r.prompt_len, rng, 64, 32768))
                output = int(_jitter(r.output_len, rng, 8, 2048))
                prefix = int(_jitter(r.prefix_len, rng, 0, max(0, prompt - 1)))
                prefix = min(prefix, max(0, prompt - 1))
                rounds.append(
                    AgentRound(
                        request_id=f"{sid}_round_{j}",
                        session_id=sid,
                        round_id=j,
                        prompt_len=prompt,
                        output_len=output,
                        prefix_len=prefix,
                        tool_latency_ms=_jitter(r.tool_latency_ms, rng, 0.0, 60000.0),
                        arrival_time=float(j),
                    )
                )
            sessions.append(AgentSession(session_id=sid, rounds=rounds))
        mode = "bootstrap"

    meta = {
        "num_sessions": len(sessions),
        "num_requests": sum(len(s.rounds) for s in sessions),
        "mode": mode,
    }
    meta.update({
        "trace_source": str(TRACE_WORKLOAD),
        "mode": mode,
        "seed": seed,
        "jitter": JITTER,
    })
    return sessions, meta
