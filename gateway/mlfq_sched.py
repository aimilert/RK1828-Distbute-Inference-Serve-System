#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端侧 MLFQ 调度内核（纯逻辑，只用标准库）。

把服务端 `algorithm/mlfq/` 的五因子调度搬到**网关的会话分配**上：当一个会话空出来、
而队列里不止一个请求在等时，不再"谁先被条件变量叫醒谁拿"，而是给每个等待者打分、取最高分。

板卡上**看不到 trace 的未来**（服务端是离线 workload，知道每个 session 还剩几轮、下一轮
工具等多久），所以五个因子全部用**可观测近似**：

    服务端因子                     端侧可观测近似
    cp 关键路径(剩余算力)          本轮估算算力 = 估算 prompt token 数 + max_tokens
    cr 完成奖励(剩余轮数)          恒 0（不可观测；归一化后是常数，不参与排序）
    sa 饥饿避免                    在队列里已等待的秒数
    pf 前缀亲和                    prompt 与候选会话 `known` 前缀的重合比例（可复用比例）
    tl 工具补偿                    1/(1+距上次提问结束的空闲秒数)（"刚回来"的对话优先；
                                   端侧只能看到上一段空闲，看不到下一轮工具延迟）

    长任务分流（MLFQ 的 Q2）：估算 prompt token 超过阈值的请求单独限流（并发上限
    `long_max`），避免长 prefill 把 decode 挤空——服务端那个阈值是 30000 token，端侧
    默认 8192，可用 `SCHED_LONG_TOKENS` 调。

打分与归一化**照抄** `algorithm/mlfq/factors.py`：对当前候选集合做 min-max 归一化后按
权重求和取最大者；某个因子在候选里全相等时记 0.5（那边就是这么写的）。权重默认用服务端
`mlfq` 那组 `(0.4, 0.3, 0.15, 0.1, 0.05)`。

重要：`fcfs` 策略**完全不进这里**——默认行为与改造前逐字节一致，只有 `SCHED_POLICY=mlfq`
才启用本模块。
"""

from __future__ import annotations

FACTOR_NAMES = ["cp", "cr", "sa", "pf", "tl"]
# 与服务端 algorithm/mlfq/config.py 的 POLICIES["mlfq"] 保持一致
DEFAULT_WEIGHTS = (0.4, 0.3, 0.15, 0.1, 0.05)
DEFAULT_LONG_TOKENS = 8192
# 端侧没有 tokenizer，用字节数估算 token：中英混排 + 代码实测 3.5~4.5 字节/token。
# 它与窗口预算那边的"3 字节/token"是同量级的粗估，只用于**相对**比较（因子归一化后只比大小）。
BYTES_PER_TOKEN = 3.5


def parse_weights(text, default=DEFAULT_WEIGHTS):
    """`"0.4,0.3,0.15,0.1,0.05"` -> 五元组；非法就回默认（启动时提示一次）。"""
    if text is None:
        return tuple(default)
    try:
        parts = [float(x) for x in str(text).replace(";", ",").split(",") if x.strip() != ""]
    except ValueError:
        return tuple(default)
    if len(parts) != len(FACTOR_NAMES):
        return tuple(default)
    return tuple(parts)


def estimate_prompt_tokens(prompt):
    """估算一段 prompt 的 token 数（字节/3.5）。至少 1，绝不返回 0。"""
    if not prompt:
        return 1.0
    return max(1.0, len(prompt.encode("utf-8")) / BYTES_PER_TOKEN)


def prefix_ratio(prompt, known):
    """本次 prompt 能以 `known` 为前缀时，可复用的字节比例（= pf 因子）。

    · known 为空/None（会话干净或内容未知）=> 0：没有可复用的东西；
    · prompt 不以 known 开头 => 0：前缀对不上，网关会 RESET 全量重发（可复用 0）；
    · 否则 = len(known)/len(prompt)。
    """
    if not known or not prompt:
        return 0.0
    if not prompt.startswith(known):
        return 0.0
    total = len(prompt.encode("utf-8"))
    if total <= 0:
        return 0.0
    return min(1.0, len(known.encode("utf-8")) / total)


def tool_latency_proxy(now, last_used):
    """tl 的端侧近似：`last_used` 是这段对话上次提问结束的时刻（None = 没见过）。

    刚回来（空闲短）说明这段对话的工具等待短、续跑便宜，给高分；没见过的新对话给 0
    （没有证据），而不是默认最高——否则新请求会一直压着老对话。
    """
    if last_used is None:
        return 0.0
    idle = max(0.0, now - last_used)
    return 1.0 / (1.0 + idle)


def normalize_and_score(cands, weights):
    """对候选集合做 min-max 归一化后加权求和。返回 {key: score}。

    `cands`: [{"key":…, "cp":float, "cr":float, "sa":float, "pf":float, "tl":float}, …]
    与 factors.py 完全一致：某因子在候选里全相等时记 0.5。
    """
    out = {}
    for c in cands:
        out[c["key"]] = 0.0
    for i, f in enumerate(FACTOR_NAMES):
        vals = [c[f] for c in cands]
        lo, hi = min(vals), max(vals)
        w = weights[i]
        for c in cands:
            norm = (c[f] - lo) / (hi - lo) if hi > lo else 0.5
            out[c["key"]] += w * norm
    return out


def choose(cands, weights, now, max_inflight_long=None, long_tokens=None,
           long_inflight=0):
    """返回 (选中 key | None, 说明字符串)。

    规则与顺序：
      1. 没有候选 => (None, "empty")；
      2. 长任务限流：超阈值的候选只有 `long_inflight < max_inflight_long` 时才可选；
         若所有候选都被限流 => (None, "long-throttled")（调用方应继续等，不派发）；
      3. 权重全 0 => FCFS：等待最久的优先；
      4. 否则按归一化加权分取最大；**同分用等待更久者**打破（避免依赖字典顺序）。
    """
    if not cands:
        return None, "empty"
    pool = cands
    if long_tokens is not None and max_inflight_long is not None:
        allowed = [c for c in pool if c["cp"] <= long_tokens or long_inflight < max_inflight_long]
        if not allowed:
            return None, "long-throttled"
        pool = allowed
    if all(w == 0.0 for w in weights):
        best = max(pool, key=lambda c: (c["sa"], c["key"]))
        return best["key"], "fcfs"
    scores = normalize_and_score(pool, weights)
    best = max(pool, key=lambda c: (scores[c["key"]], c["sa"]))
    return best["key"], "score=%.4f" % scores[best["key"]]


# ============================================================================
# 可插拔优先级策略（除 MLFQ 外）
#
# 说明：本栈是**非抢占、会话级**分配——只在"一个会话空出来、队列里不止一人等"时选谁先拿。
# 因此这些策略主要改 **TTFT/公平性**，不改峰值吞吐（吞吐由流水线瓶颈决定）。
# 统一候选字段（缺省有安全默认）：
#   key, cp(估算算力), cr, sa(等待秒), pf(前缀亲和), tl(工具空闲代理)
#   svc(已获服务量, LAS/Stride 用), dl(截止期 slack 秒, EDF 用), cls(优先级类, 越大越高), wq(权重)
# ============================================================================
POLICIES = ("fcfs", "mlfq", "sjf", "las", "stride", "edf", "prio", "prefix")


def _c_flat(c, name, default=0.0):
    v = c.get(name, default)
    return default if v is None else v


def pick(policy, cands, weights, now, max_inflight_long=None, long_tokens=None,
         long_inflight=0):
    """按策略选一个 key。语义与 choose() 一致（长任务限流 + 空集）。

    · fcfs   : 等待最久优先（= MLFQ 权重全 0 的退化，作基线）
    · mlfq   : 现有五因子加权（见 choose）
    · sjf    : cp 最小（最短作业优先，均延迟最优；同分等更久者）
    · las    : svc 最小（Least-Attained-Service：已获得服务最少者优先，重尾负载友好）
    · stride : svc/wq 最小（加权公平：虚拟运行时间，按权重分份额）
    · edf    : dl 最小（最近截止期优先；slack=deadline-now）
    · prio   : cls 最大（严格优先级类；同分等更久者）
    · prefix : pf 最大（前缀命中优先，省 prefill；同分等更久者）
    """
    if not cands:
        return None, "empty"
    pool = cands
    if long_tokens is not None and max_inflight_long is not None:
        allowed = [c for c in pool if c["cp"] <= long_tokens or long_inflight < max_inflight_long]
        if not allowed:
            return None, "long-throttled"
        pool = allowed

    p = (policy or "fcfs").lower()
    if p == "mlfq":
        return choose(pool, weights, now, max_inflight_long, long_tokens, long_inflight)
    if p == "sjf":
        best = min(pool, key=lambda c: (_c_flat(c, "cp"), -_c_flat(c, "sa")))
    elif p == "las":
        best = min(pool, key=lambda c: (_c_flat(c, "svc"), -_c_flat(c, "sa")))
    elif p == "stride":
        best = min(pool, key=lambda c: (_c_flat(c, "svc") / max(1e-9, _c_flat(c, "wq", 1.0)),
                                        -_c_flat(c, "sa")))
    elif p == "edf":
        best = min(pool, key=lambda c: (_c_flat(c, "dl", 1e18), -_c_flat(c, "sa")))
    elif p == "prio":
        best = max(pool, key=lambda c: (_c_flat(c, "cls"), _c_flat(c, "sa")))
    elif p == "prefix":
        best = max(pool, key=lambda c: (_c_flat(c, "pf"), _c_flat(c, "sa")))
    else:  # fcfs / 未知
        best = max(pool, key=lambda c: (_c_flat(c, "sa"), c["key"]))
    return best["key"], "policy=%s" % p


def selftest(check):
    """纯逻辑自检（不碰网关、不碰后端）。"""
    now = 1000.0
    # 1) FCFS：权重全 0 时取等待最久
    cands = [{"key": "a", "cp": 10, "cr": 0, "sa": 1, "pf": 0, "tl": 0},
             {"key": "b", "cp": 10, "cr": 0, "sa": 9, "pf": 0, "tl": 0}]
    k, why = choose(cands, (0.0, 0.0, 0.0, 0.0, 0.0), now)
    check("FCFS 取等待最久", k == "b", "%s (%s)" % (k, why))

    # 2) sa 单因子：等待越久分越高
    k, _ = choose(cands, (0.0, 0.0, 1.0, 0.0, 0.0), now)
    check("sa 取等待最久", k == "b", k)

    # 3) cp 单因子：估算算力越大越优先（与服务端 factors.py 同向：max cp）
    cands_cp = [{"key": "short", "cp": 100, "cr": 0, "sa": 5, "pf": 0, "tl": 0},
                {"key": "long", "cp": 900, "sa": 0, "cr": 0, "pf": 0, "tl": 0}]
    k, _ = choose(cands_cp, (1.0, 0.0, 0.0, 0.0, 0.0), now)
    check("cp 取算力更大者", k == "long", k)

    # 4) pf 单因子：可复用比例高者优先
    cands_pf = [{"key": "cold", "cp": 0, "cr": 0, "sa": 0, "pf": 0.0, "tl": 0},
                {"key": "hot", "cp": 0, "cr": 0, "sa": 0, "pf": 0.9, "tl": 0}]
    k, _ = choose(cands_pf, (0.0, 0.0, 0.0, 1.0, 0.0), now)
    check("pf 取前缀亲和高者", k == "hot", k)

    # 5) 长任务限流：唯一候选是长任务且已到并发上限 => 不派发
    long_c = [{"key": "L", "cp": 20000, "cr": 0, "sa": 1, "pf": 0, "tl": 0}]
    k, why = choose(long_c, (1.0, 0.0, 0.0, 0.0, 0.0), now,
                    max_inflight_long=1, long_tokens=8192, long_inflight=1)
    check("长任务到上限时不派发", k is None and why == "long-throttled", why)
    k, _ = choose(long_c, (1.0, 0.0, 0.0, 0.0, 0.0), now,
                  max_inflight_long=1, long_tokens=8192, long_inflight=0)
    check("长任务未到上限可派发", k == "L", k)
    # 短任务不受限流影响
    mix = [{"key": "L", "cp": 20000, "sa": 0, "cr": 0, "pf": 0, "tl": 0},
           {"key": "S", "cp": 100, "sa": 0, "cr": 0, "pf": 0, "tl": 0}]
    k, _ = choose(mix, (1.0, 0.0, 0.0, 0.0, 0.0), now,
                  max_inflight_long=1, long_tokens=8192, long_inflight=1)
    check("限流时长任务被排除、短任务可选", k == "S", k)

    # 6) 估算与前缀比例
    check("prompt token 估算 > 0", estimate_prompt_tokens("abc" * 100) > 0)
    check("前缀命中比例 = len(known)/len(prompt)",
          abs(prefix_ratio("x" * 100, "x" * 50) - 0.5) < 1e-9)
    check("前缀不命中 = 0", prefix_ratio("abcdef", "zzz") == 0.0)
    check("known 为空 = 0", prefix_ratio("abc", "") == 0.0)

    # 7) 权重解析
    check("权重解析", parse_weights("0.4,0.3,0.15,0.1,0.05") == (0.4, 0.3, 0.15, 0.1, 0.05))
    check("非法权重回默认", parse_weights("1,2") == DEFAULT_WEIGHTS)

    # 8) 可插拔策略
    w0 = (0.0, 0.0, 0.0, 0.0, 0.0)
    C = [{"key": "L", "cp": 900, "cr": 0, "sa": 1, "pf": 0.0, "tl": 0,
          "svc": 500, "dl": 9.0, "cls": 0, "wq": 1.0},
         {"key": "S", "cp": 100, "cr": 0, "sa": 0, "pf": 0.0, "tl": 0,
          "svc": 50, "dl": 2.0, "cls": 0, "wq": 1.0}]
    check("fcfs 取等待最久(L)", pick("fcfs", C, w0, now)[0] == "L")
    check("sjf 取 cp 最小(S)", pick("sjf", C, w0, now)[0] == "S")
    check("las 取 svc 最小(S)", pick("las", C, w0, now)[0] == "S")
    check("edf 取 slack 最小(S)", pick("edf", C, w0, now)[0] == "S")
    check("prefix 取 pf 最大",
          pick("prefix", [{"key": "a", "cp": 0, "sa": 0, "pf": 0.1},
                          {"key": "b", "cp": 0, "sa": 0, "pf": 0.9}], w0, now)[0] == "b")
    check("stride 按 svc/wq",
          pick("stride", [{"key": "A", "cp": 0, "sa": 0, "svc": 100, "wq": 10.0},
                          {"key": "B", "cp": 0, "sa": 0, "svc": 50, "wq": 1.0}], w0, now)[0] == "A")
    check("prio 取高类",
          pick("prio", [{"key": "lo", "cp": 0, "sa": 99, "cls": 0},
                        {"key": "hi", "cp": 0, "sa": 0, "cls": 1}], w0, now)[0] == "hi")
    check("长任务限流对 pick 同样生效",
          pick("sjf", [{"key": "L", "cp": 20000, "sa": 1}], w0, now,
               max_inflight_long=1, long_tokens=8192, long_inflight=1)[0] is None)
