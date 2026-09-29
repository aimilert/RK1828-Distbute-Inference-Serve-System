"""针对串行链 agent workload 的打分内核。

每个 session 是一条严格串行的 round 链, 静态因子按轮预计算:
    cp[i] = Σ_{j>=i} est(round[j])           关键路径(剩余算力)
    cr[i] = len(rounds) - i                  完成奖励(剩余轮数, 含自身)
    pf[i] = prefix_len / prompt_len           前缀亲和(KV 可复用比例)
    tl[i] = 1 / (1 + tool_latency_ms / 1000) 工具补偿(下一轮等待越短越优先)
动态因子:
    sa = now - ready_time                     饥饿避免(等待时长)

score = Σ w_f · norm_f(factor), 对当前 ready 集合做 min-max 归一化。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import EST_DECODE_TOK_S, EST_PREFILL_TOK_S, FACTOR_NAMES
from workload import AgentRound, AgentSession


def _est_time(r: AgentRound) -> float:
    return r.prompt_len / EST_PREFILL_TOK_S + r.output_len / EST_DECODE_TOK_S


@dataclass
class FactorGraph:
    """工作负载的因子图, 提供串行链的静态预计算与动态打分。"""

    sessions: dict[str, AgentSession] = field(default_factory=dict)
    cp: dict[str, float] = field(default_factory=dict)
    cr: dict[str, float] = field(default_factory=dict)
    pf: dict[str, float] = field(default_factory=dict)
    tl: dict[str, float] = field(default_factory=dict)

    @classmethod
    def build(cls, sessions: list[AgentSession]) -> "FactorGraph":
        g = cls()
        for s in sessions:
            g.sessions[s.session_id] = s
            n = len(s.rounds)
            suffix = 0.0
            for i in range(n - 1, -1, -1):
                r = s.rounds[i]
                suffix += _est_time(r)
                g.cp[r.request_id] = suffix
                g.cr[r.request_id] = float(n - i)
                g.pf[r.request_id] = (
                    r.prefix_len / r.prompt_len if r.prompt_len > 0 else 0.0
                )
                g.tl[r.request_id] = 1.0 / (1.0 + r.tool_latency_ms / 1000.0)
        return g

    def factor_values(
        self, rid: str, now: float, ready_time: float
    ) -> dict[str, float]:
        return {
            "cp": self.cp[rid],
            "cr": self.cr[rid],
            "sa": now - ready_time,
            "pf": self.pf[rid],
            "tl": self.tl[rid],
        }

    def score(
        self,
        rid: str,
        weights: tuple[float, float, float, float, float],
        now: float,
        ready_time_map: dict[str, float],
        ready_ids: list[str],
    ) -> float:
        raw = {x: self.factor_values(x, now, ready_time_map.get(x, now)) for x in ready_ids}
        norm: dict[str, dict[str, float]] = {x: {} for x in ready_ids}
        for f in FACTOR_NAMES:
            vals = [raw[x][f] for x in ready_ids]
            lo, hi = min(vals), max(vals)
            for x in ready_ids:
                norm[x][f] = (raw[x][f] - lo) / (hi - lo) if hi > lo else 0.5
        w = dict(zip(FACTOR_NAMES, weights))
        return sum(w[f] * norm[rid][f] for f in FACTOR_NAMES)
