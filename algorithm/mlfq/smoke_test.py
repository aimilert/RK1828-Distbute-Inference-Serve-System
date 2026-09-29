"""离线冒烟测试: 用 mock 客户端验证上层调度逻辑(无需 GPU / vLLM 服务)。"""

from __future__ import annotations

import asyncio
import time

from factors import FactorGraph
from scheduler import PromptCache, run_policy
from vllm_client import StreamResult
from workload import build_workload


class MockClient:
    async def stream_chat(self, prompt, *, max_tokens, temperature, request_start_time):
        # 模拟: TTFT 与 prompt 长度相关, decode 与 output 长度相关
        ttft = 0.01 + len(prompt) / 200000.0
        decode = max_tokens * 0.002
        await asyncio.sleep(ttft + decode)
        r = StreamResult()
        r.status = 200
        r.output_tokens = max_tokens
        r.first_token_time = request_start_time + ttft
        r.request_end_time = time.monotonic()
        return r

    async def close(self):
        pass


async def main() -> None:
    sessions, meta = build_workload(2)  # 用真实 2 个 session
    print("workload meta:", meta)
    g = FactorGraph.build(sessions)
    for s in sessions[:2]:
        r = s.rounds[0]
        print(
            f"  {s.session_id}: rounds={len(s.rounds)} "
            f"first prompt={r.prompt_len} out={r.output_len} prefix={r.prefix_len} "
            f"tool={r.tool_latency_ms}ms"
        )

    pc = PromptCache(None)
    policies = [
        ("fcfs", (0.0, 0.0, 0.0, 0.0, 0.0)),
        ("cp", (1.0, 0.0, 0.0, 0.0, 0.0)),
        ("mlfq", (0.4, 0.3, 0.15, 0.1, 0.05)),
    ]
    for name, w in policies:
        res = await run_policy(
            sessions,
            w,
            client=MockClient(),
            tokenizer=None,
            concurrency=4,
            prompt_cache=pc,
            enable_long_split=(name == "mlfq"),
        )
        s = res.summary
        ok, total = s["ok"], s["total_requests"]
        print(
            f"policy {name}: ok={ok}/{total} makespan={s['makespan']:.2f}s "
            f"bubble={s['bubble']['bubble_ratio']:.4f} "
            f"throughput={s['throughput_req_s']:.2f} req/s"
        )
        assert ok == total, f"FAILED: {name} has {total - ok} failed requests"
    print("OFFLINE SANITY PASS")


if __name__ == "__main__":
    asyncio.run(main())
