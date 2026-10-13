#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8K 换页的端到端净收益测量（多会话）。

场景：建 N 段"接近满 8K"的对话 → 静默让 idle 回收把它们换出（`_swap_out`）→ 逐段带
追问回来（`_swap_in`）→ 对比**同一段对话**的两次 prefill：

  · 建轮 prefill_ms = 该上下文的**全量 prefill**（= 不换页时"丢弃后重算"的代价）；
  · 恢复轮 prefill_ms = 换页读回后只补**增量**的 prefill。

净收益 = 建轮 prefill_ms − 恢复轮 prefill_ms（**用 /v1/chat/completions 响应里的
`perf.prefill_ms` 直接取，已剥离排队/网络**）。每段对话埋一个唯一暗号，顺带验证读回的是
真 KV。

只用标准库；可在任意能连到板卡的机器上跑。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

MATERIAL = "边缘推理的多会话调度需要在算力、显存与公平性之间取平衡。"
ASK = "请只回答暗号本身，不要解释。"


def chat(base, conv, messages, max_tokens, timeout=1800):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Conversation-Id": conv},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(base, path, timeout=30):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="8K 换页净收益（多会话）")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--label", default="kv8k")
    ap.add_argument("--conv", type=int, default=8, help="对话段数")
    ap.add_argument("--repeat", type=int, default=450, help="建轮材料重复次数（≈满 8K）")
    ap.add_argument("--idle", type=float, default=8.0, help="建完后的静默秒数")
    ap.add_argument("--build-max-tokens", type=int, default=8)
    ap.add_argument("--ask-max-tokens", type=int, default=256,
                    help="恢复轮 max_tokens；模型会先输出 <think>，给足才看得到暗号")
    ap.add_argument("--summary-only", action="store_true", help="只打印汇总，不打印每段明细")
    args = ap.parse_args()

    material = MATERIAL * args.repeat
    rows = []
    replies = {}

    # 1) 建 N 段对话（每段一个唯一暗号）
    for i in range(args.conv):
        cw = "CODE-%04d" % i
        big = "请阅读并记住材料，其中有一个暗号。\n【暗号：%s】\n%s" % (cw, material)
        t0 = time.time()
        d = chat(args.base, "%s-%02d" % (args.label, i),
                 [{"role": "user", "content": big}], args.build_max_tokens)
        perf = d.get("perf", {})
        replies[i] = d["choices"][0]["message"].get("content") or ""
        rows.append({"conv": i, "codeword": cw,
                     "build_prefill_ms": perf.get("prefill_ms"),
                     "build_prefill_tokens": perf.get("prefill_tokens"),
                     "build_ctx_tokens": perf.get("ctx_tokens"),
                     "build_queue_wait_s": perf.get("queue_wait_s"),
                     "build_wall_s": round(time.time() - t0, 2)})
        print("build %d: prefill_ms=%s tok=%s ctx=%s wall=%.1fs"
              % (i, perf.get("prefill_ms"), perf.get("prefill_tokens"),
                 perf.get("ctx_tokens"), rows[-1]["build_wall_s"]))

    # 2) 静默 > CONTEND_IDLE，让 idle 回收把它们换出
    time.sleep(args.idle)

    # 3) 逐段带追问回来（触发 swap_in）
    for i in range(args.conv):
        cw = rows[i]["codeword"]
        big = "请阅读并记住材料，其中有一个暗号。\n【暗号：%s】\n%s" % (cw, material)
        msgs = [{"role": "user", "content": big},
                {"role": "assistant", "content": replies[i]},
                {"role": "user", "content": ASK}]
        d = chat(args.base, "%s-%02d" % (args.label, i), msgs, args.ask_max_tokens)
        perf = d.get("perf", {})
        text = d["choices"][0]["message"].get("content") or ""
        rows[i]["resume_prefill_ms"] = perf.get("prefill_ms")
        rows[i]["resume_prefill_tokens"] = perf.get("prefill_tokens")
        rows[i]["resume_reset"] = perf.get("reset")
        rows[i]["resume_cached_tokens"] = perf.get("cached_tokens")
        rows[i]["resume_queue_wait_s"] = perf.get("queue_wait_s")
        rows[i]["resume_has_codeword"] = cw in text
        if rows[i].get("build_prefill_ms") is not None and perf.get("prefill_ms") is not None:
            rows[i]["saved_ms"] = round(rows[i]["build_prefill_ms"] - perf["prefill_ms"], 3)
        print("resume %d: prefill_ms=%s reset=%s cached=%s codeword=%s"
              % (i, perf.get("prefill_ms"), perf.get("reset"),
                 perf.get("cached_tokens"), rows[i]["resume_has_codeword"]))

    builds = [r["build_prefill_ms"] for r in rows if r.get("build_prefill_ms") is not None]
    resumes = [r["resume_prefill_ms"] for r in rows if r.get("resume_prefill_ms") is not None]
    saved = [r["saved_ms"] for r in rows if r.get("saved_ms") is not None]
    try:
        pool = get(args.base, "/v1/pool")
    except Exception:                                           # noqa: BLE001
        pool = {}
    summary = {
        "label": args.label, "conv": args.conv, "repeat": args.repeat,
        "n": len(rows),
        "swap_out_total": pool.get("swap_out_total"),
        "swap_in_total": pool.get("swap_in_total"),
        "swapped_now": pool.get("swapped"),
        "avg_build_prefill_ms": round(sum(builds) / len(builds), 1) if builds else None,
        "avg_resume_prefill_ms": round(sum(resumes) / len(resumes), 1) if resumes else None,
        "avg_saved_ms": round(sum(saved) / len(saved), 1) if saved else None,
        "avg_saved_pct": (round(100.0 * sum(saved) / sum(builds), 1)
                          if builds and saved else None),
        "total_saved_s": round(sum(saved) / 1000.0, 1) if saved else None,
        "n_reset0_kv_reused": sum(1 for r in rows if r.get("resume_reset") is False
                                  or r.get("resume_reset") == 0),
        "n_codeword_ok": sum(1 for r in rows if r.get("resume_has_codeword")),
        "rows": rows,
    }
    if args.summary_only:
        summary.pop("rows", None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
