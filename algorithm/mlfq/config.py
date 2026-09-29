"""上层封装调度实验配置。

针对 TraceLab agent workload(多会话串行链 + 工具延迟长尾) 的上层调度器:
在 agent 调度之上封装一层"跨会话优先级 + 工具等待补位 + 长任务分流"。

五个调度因子:
    cp 关键路径 Critical Path  —— 该 session 剩余链的预估算力(prefill+decode)
    cr 完成奖励 Completion Reward —— 该 session 剩余 round 数
    sa 饥饿避免 Starvation Avoid  —— 该 round 自 ready 起已等待的时长
    pf 前缀亲和 Prefix Affinity   —— prefix_len / prompt_len(KV 可复用比例)
    tl 工具补偿 Tool-latency      —— 下一轮工具延迟越短优先级越高(快速周转)
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(os.environ.get("SCHED_RESULTS_DIR", BASE_DIR / "results"))

# TraceLab 真实 agent workload(19 round / 2 sessions)
TRACE_WORKLOAD = Path(os.environ.get(
    "SCHED_TRACE_WORKLOAD",
    BASE_DIR / "my_real_workload.jsonl",
))

VLLM_BASE_URL = os.environ.get("SCHED_VLLM_BASE_URL", "http://127.0.0.1:8000")
MODEL_NAME = os.environ.get("SCHED_MODEL_NAME", "Qwen3-8B")
TOKENIZER_PATH = os.environ.get("SCHED_TOKENIZER_PATH", "Qwen3-8B")

CONCURRENCY = 8
TEMPERATURE = 0.0
API_TIMEOUT_S = 1200.0
TOOL_WAIT_SCALE = float(os.environ.get("SCHED_TOOL_WAIT_SCALE", "1.0"))

# 负载: 默认用真实 2 个 session; 大于真实数时从真实分布 bootstrap 合成
NUM_SESSIONS = 20
JITTER = 0.15
BOOTSTRAP_SEED = 20260902

# 预估算力参数(tok/s)
EST_PREFILL_TOK_S = 8000.0
EST_DECODE_TOK_S = 40.0

# 长任务分流(MLFQ 的 Q2 队列): prompt 超过阈值的 round 单独限流
# 注意: 并发=1 会饿死 GPU(实测 bubble 0.74), 用并发的一半做温和限流
LONG_PROMPT_THRESHOLD = 30000
LONG_PROMPT_MAX_CONCURRENT = 4

# 服务端上下文窗口(prompt 会被裁剪到 max_model_len - output - margin)
MAX_MODEL_LEN = 32768
CHAT_TEMPLATE_MARGIN = 32

FACTOR_NAMES = ["cp", "cr", "sa", "pf", "tl"]
FACTOR_LABELS = {
    "cp": "关键路径 Critical Path",
    "cr": "完成奖励 Reward",
    "sa": "饥饿避免 Anti-starvation",
    "pf": "前缀亲和 Prefix Affinity",
    "tl": "工具补偿 Tool-latency",
}

# 策略权重 (cp, cr, sa, pf, tl); mlfq 额外启用长任务分流
POLICIES: dict[str, tuple[float, float, float, float, float]] = {
    "fcfs":     (0.0, 0.0, 0.0, 0.0, 0.0),
    "cp":       (1.0, 0.0, 0.0, 0.0, 0.0),
    "cr":       (0.0, 1.0, 0.0, 0.0, 0.0),
    "sa":       (0.0, 0.0, 1.0, 0.0, 0.0),
    "pf":       (0.0, 0.0, 0.0, 1.0, 0.0),
    "tl":       (0.0, 0.0, 0.0, 0.0, 1.0),
    "balanced": (0.5, 0.3, 0.2, 0.0, 0.0),
    "mlfq":     (0.4, 0.3, 0.15, 0.1, 0.05),
}
