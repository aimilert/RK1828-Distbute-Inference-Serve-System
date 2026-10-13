#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""③ 请求头优先级验证：同一时刻排队的两段对话，带 `X-Priority` 的应更先拿到会话。

方法：先起 N 个"占位"对话占满会话（长生成）；随后**同时**发一对请求——LOW（默认）与
HIGH（X-Priority=1）——它们都要排队，调度器在会话空出时按策略挑选。比较两者 queue_wait。
判据：`SCHED_POLICY=prio` 时 HIGH 的 queue_wait 明显小于 LOW；`fcfs` 时两者相近（头被忽略）。
只用标准库。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request

MATERIAL = "边缘算力平台的调度优化需要在算力显存与公平性之间取得平衡。"


def chat(base, conv, prompt, max_tokens, prio=0, weight=None, deadline_ms=None, out=None):
    h = {"Content-Type": "application/json", "X-Conversation-Id": conv}
    if prio:
        h["X-Priority"] = str(prio)
    if weight is not None:
        h["X-Weight"] = str(weight)
    if deadline_ms is not None:
        h["X-Deadline-Ms"] = str(deadline_ms)
    body = {"model": "qwen3.5-27b", "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=h, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read().decode("utf-8"))
    perf = d.get("perf", {})
    rec = {"conv": conv, "prio": prio, "wall_s": round(time.time() - t0, 2),
           "queue_wait_s": perf.get("queue_wait_s"), "ttft_ms": perf.get("ttft_ms"),
           "e2e_ms": perf.get("e2e_ms")}
    if out is not None:
        out.append(rec)
    return rec


def main():
    ap = argparse.ArgumentParser(description="③ 请求头优先级验证")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--fillers", type=int, default=2, help="占位对话数（应=NSESSION）")
    ap.add_argument("--repeat", type=int, default=120)
    ap.add_argument("--filler-tokens", type=int, default=400)
    ap.add_argument("--pair-tokens", type=int, default=16)
    ap.add_argument("--settle", type=float, default=2.0)
    args = ap.parse_args()

    big = MATERIAL * args.repeat
    holders = []

    def hold(i):
        chat(args.base, "fill-%d" % i, big, args.filler_tokens, out=holders)

    ths = [threading.Thread(target=hold, args=(i,)) for i in range(args.fillers)]
    for t in ths:
        t.start()
    time.sleep(args.settle)   # 让占位对话进入生成、稳住会话

    results = []
    t_low = threading.Thread(target=chat, args=(args.base, "pair-LOW", "你好", args.pair_tokens), kwargs={"prio": 0, "out": results})
    t_high = threading.Thread(target=chat, args=(args.base, "pair-HIGH", "你好", args.pair_tokens), kwargs={"prio": 1, "out": results})
    t_low.start()
    t_high.start()
    t_low.join()
    t_high.join()
    for t in ths:
        t.join()

    by = {r["conv"]: r for r in results}
    low = by.get("pair-LOW", {})
    high = by.get("pair-HIGH", {})
    out = {
        "fillers": args.fillers,
        "LOW": {"queue_wait_s": low.get("queue_wait_s"), "ttft_ms": low.get("ttft_ms")},
        "HIGH": {"queue_wait_s": high.get("queue_wait_s"), "ttft_ms": high.get("ttft_ms")},
        "HIGH_before_LOW": (high.get("queue_wait_s") is not None and low.get("queue_wait_s") is not None
                            and high["queue_wait_s"] < low["queue_wait_s"]),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
