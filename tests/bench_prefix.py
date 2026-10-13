#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⑥ 核实：跨会话的公共前缀（system）会不会被复用？

发送两段**不同对话**、但带**相同长 system 前缀**的请求：
  · P2（跨会话）：与 P1 共享 system 前缀 —— 期望当前实现**不复用**（reset=1，全量 prefill）。
  · P1 追问（同会话）：同一对话的续轮 —— 期望**复用**（reset=0，只发增量）。
两者对比即说明"复用是**按会话**的，跨会话共享前缀不生效"。只用标准库。
"""
from __future__ import annotations

import argparse
import json
import urllib.request

MATERIAL = "边缘算力平台的调度优化需要在算力显存与公平性之间取得平衡。"


def chat(base, conv, messages, max_tokens=8):
    body = {"model": "qwen3.5-27b", "messages": messages, "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "X-Conversation-Id": conv}, method="POST")
    with urllib.request.urlopen(req, timeout=1200) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d


def perf_of(d):
    p = d.get("perf", {})
    return {"reset": p.get("reset"), "prefill_tokens": p.get("prefill_tokens"),
            "prefill_ms": p.get("prefill_ms"), "cached_tokens": p.get("cached_tokens"),
            "ctx_tokens": p.get("ctx_tokens")}


def main():
    ap = argparse.ArgumentParser(description="跨会话公共前缀复用核实")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--repeat", type=int, default=100)
    args = ap.parse_args()

    SYS = "你是一个遵循以下规范的助手。\n" + MATERIAL * args.repeat
    q1 = "问题一：请只回答数字 1"
    q2 = "问题二：请只回答数字 2"

    p1 = chat(args.base, "pfx-1", [{"role": "system", "content": SYS},
                                   {"role": "user", "content": q1}])
    a1 = p1["choices"][0]["message"].get("content") or ""
    # 跨会话：不同 key、相同 system 前缀
    p2 = chat(args.base, "pfx-2", [{"role": "system", "content": SYS},
                                   {"role": "user", "content": q2}])
    # 同会话：P1 的续轮（应复用）
    p1b = chat(args.base, "pfx-1", [{"role": "system", "content": SYS},
                                    {"role": "user", "content": q1},
                                    {"role": "assistant", "content": a1},
                                    {"role": "user", "content": "再回答一次"}])

    out = {
        "system_prefix_tokens_est": len(SYS.encode("utf-8")) / 3.5,
        "P2_cross_conversation": perf_of(p2),
        "P1_followup_within_conversation": perf_of(p1b),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
