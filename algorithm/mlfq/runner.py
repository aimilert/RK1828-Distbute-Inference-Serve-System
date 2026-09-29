"""主执行器: 构建 agent workload, 依次运行各调度策略, 落盘结果。

用法(在 yl-demo 容器内, /opt/conda/bin/python):
    python runner.py --policies fcfs,cp,cr,sa,pf,tl,balanced,mlfq --num-sessions 20 --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from config import (
    CONCURRENCY,
    MODEL_NAME,
    NUM_SESSIONS,
    POLICIES,
    RESULTS_DIR,
    TEMPERATURE,
    TOKENIZER_PATH,
    VLLM_BASE_URL,
)
from monitor import GPUMonitor
from scheduler import PromptCache, run_policy
from vllm_client import VLLMClient
from workload import build_workload


def load_tokenizer(path: str):
    if not path or not Path(path).exists():
        print("NOTE: 未找到 tokenizer, 使用字符近似 prompt 生成器")
        return None
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
        print(f"Loaded Qwen3 tokenizer from {path}")
        return tok
    except Exception as exc:
        print(f"WARNING: 加载 tokenizer 失败, 回退字符近似: {exc}")
        return None


async def main(policies: list[str], num_sessions: int, concurrency: int) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    sessions, meta = build_workload(num_sessions)
    print(
        f"Workload: {meta['num_requests']} requests across "
        f"{meta['num_sessions']} sessions (mode={meta['mode']})"
    )

    tokenizer = load_tokenizer(TOKENIZER_PATH)
    client = VLLMClient(base_url=VLLM_BASE_URL, model=MODEL_NAME)
    await client.check_connection()
    info = await client.get_model_info()
    max_model_len = info.get("max_model_len") or None
    print(
        f"Connected to vLLM {VLLM_BASE_URL}; model={info.get('id')}; "
        f"max_model_len={max_model_len}"
    )

    prompt_cache = PromptCache(tokenizer)

    results_csv = RESULTS_DIR / "results.csv"
    csv_fields = [
        "policy", "weights", "makespan", "throughput_req_s", "output_tokens_s",
        "e2e_p50", "e2e_p99", "ttft_p50", "ttft_p99", "tpot_p50",
        "queuing_p50", "bubble_ratio", "tool_wait_seconds", "prefix_reuse_ratio",
        "gpu_util_mean", "kv_cache_usage_mean", "num_running_mean", "prefix_cache_hit_rate",
    ]
    with results_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()

        for name in policies:
            if name not in POLICIES:
                print(f"跳过未知策略: {name}")
                continue
            weights = POLICIES[name]
            enable_long_split = name == "mlfq"
            print(
                f"\n=== policy={name} weights={weights} long_split={enable_long_split} ===",
                flush=True,
            )

            monitor = GPUMonitor(VLLM_BASE_URL, interval_seconds=1.0)
            await monitor.start()
            result = await run_policy(
                sessions,
                weights,
                client=client,
                tokenizer=tokenizer,
                concurrency=concurrency,
                prompt_cache=prompt_cache,
                temperature=TEMPERATURE,
                max_model_len=max_model_len,
                enable_long_split=enable_long_split,
            )
            await monitor.stop()

            result.policy = name
            result.summary["gpu"] = monitor.summary()
            result.summary["enable_long_split"] = enable_long_split

            out_dir = RESULTS_DIR / name
            out_dir.mkdir(parents=True, exist_ok=True)
            with (out_dir / "request_metrics.jsonl").open("w", encoding="utf-8") as mf:
                for m in result.metrics:
                    mf.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")
            with (out_dir / "summary.json").open("w", encoding="utf-8") as sf:
                json.dump(result.to_dict(), sf, ensure_ascii=False, indent=2)

            s = result.summary
            gpu = s["gpu"]
            row = {
                "policy": name,
                "weights": json.dumps(list(weights)),
                "makespan": s["makespan"],
                "throughput_req_s": s["throughput_req_s"],
                "output_tokens_s": s["output_tokens_s"],
                "e2e_p50": s["e2e"]["p50"],
                "e2e_p99": s["e2e"]["p99"],
                "ttft_p50": s["ttft"]["p50"],
                "ttft_p99": s["ttft"]["p99"],
                "tpot_p50": s["tpot"]["p50"],
                "queuing_p50": s["queuing"]["p50"],
                "bubble_ratio": s["bubble"]["bubble_ratio"],
                "tool_wait_seconds": s["tool_wait_seconds"],
                "prefix_reuse_ratio": s["prefix_reuse_ratio"],
                "gpu_util_mean": gpu["gpu_util"]["mean"],
                "kv_cache_usage_mean": gpu["kv_cache_usage_perc"]["mean"],
                "num_running_mean": gpu["num_requests_running"]["mean"],
                "prefix_cache_hit_rate": gpu["prefix_cache_hit_rate"],
            }
            writer.writerow(row)
            fh.flush()

            print(
                f"  makespan={s['makespan']:.2f}s e2e_p50={s['e2e']['p50']:.3f}s "
                f"e2e_p99={s['e2e']['p99']:.3f}s out={s['output_tokens_s']:.1f} tok/s "
                f"bubble={s['bubble']['bubble_ratio']:.4f} "
                f"gpu_util={gpu['gpu_util']['mean']}%"
            )

    await client.close()
    print(f"\n全部完成。结果目录: {RESULTS_DIR}")
    print(f"汇总表: {results_csv}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--policies", default=",".join(POLICIES.keys()))
    parser.add_argument("--num-sessions", type=int, default=NUM_SESSIONS)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args()
    names = [p.strip() for p in args.policies.split(",") if p.strip()]
    try:
        asyncio.run(main(names, args.num_sessions, args.concurrency))
    except KeyboardInterrupt:
        sys.exit(130)
