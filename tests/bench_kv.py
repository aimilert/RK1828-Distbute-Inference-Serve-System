#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换页 A/B（暗号）驱动：验证"非活跃会话 KV 换页后被读回的是真 KV，而不是重新 prefill"，
并顺带量恢复轮 TTFT。也是 A4 的对照夹具（配合执行器 `--kv-single-seg`）。

在 **NSESSION=1** 下用三段对话制造"稳、换、回"：
  1) 对话 A 建立长上下文（里面埋一个暗号），记下模型回复原文；
  2) 静默 > CONTEND_IDLE；对话 B 到达 → 触发回收 A（`_swap_out`，A 被换出）；
  3) 静默；对话 A 带着"复述暗号"回来 → `_swap_in` 读回 A 的 KV，只增量 prefill 那一问。

判据：
  · 恢复轮 **答出暗号** 且 `X-KV-Reuse reset=0` ⇒ 读回的是真 KV；
  · 恢复轮 TTFT ≪ 同尺寸"冷"轮（把整段拼在一个请求里全量 prefill）⇒ 换页确实省了 prefill。

只读 HTTP，纯标准库；可在任意能连到板卡的机器上跑（不必在板上，除 A=板上跑之外）。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

MATERIAL = ("边缘推理的多会话调度需要在算力、显存与公平性之间取平衡。")


def _req(base, path, body, conv, stream):
    headers = {"Content-Type": "application/json"}
    if conv is not None:
        headers["X-Conversation-Id"] = conv
    if stream:
        headers["Accept"] = "text/event-stream"
    return urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                  headers=headers, method="POST")


def full_turn(base, conv, messages, max_tokens=8, timeout=1800):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": False}
    with urllib.request.urlopen(_req(base, "/v1/chat/completions", body, conv, False),
                                timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d["choices"][0]["message"].get("content") or ""


def stream_turn(base, conv, messages, max_tokens=32, timeout=1800):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": True}
    t0 = time.time()
    ttft = None
    parts = []
    with urllib.request.urlopen(_req(base, "/v1/chat/completions", body, conv, True),
                                timeout=timeout) as r:
        reuse = r.headers.get("X-KV-Reuse")
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta:
                if ttft is None:
                    ttft = time.time() - t0
                parts.append(delta)
    return {"ttft_s": ttft, "text": "".join(parts), "reuse": reuse}


def main():
    ap = argparse.ArgumentParser(description="KV 换页 A/B（暗号）")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--label", default="kv")
    ap.add_argument("--repeat", type=int, default=120, help="材料重复次数（决定上下文体量）")
    ap.add_argument("--idle", type=float, default=6.0, help="> CONTEND_IDLE 以触发回收")
    ap.add_argument("--max-tokens", type=int, default=256,
                    help="恢复轮/冷轮的 max_tokens（模型会先输出 <think>，给足才看得到正文）")
    ap.add_argument("--codeword", default="ZEBRA-7731")
    args = ap.parse_args()

    material = MATERIAL * args.repeat
    big = "请阅读并记住下面的材料，其中有一个暗号。\n【暗号：%s】\n%s" % (args.codeword, material)
    ask = "请只回答暗号本身，不要解释。"

    a = args.label + "-A"
    b = args.label + "-B"

    # 1) A 建立上下文
    a1 = full_turn(args.base, a, [{"role": "user", "content": big}], max_tokens=8)
    # 2) 静默 → B 抢占使 A 被换出
    time.sleep(args.idle)
    full_turn(args.base, b, [{"role": "user", "content": "你好"}], max_tokens=4)
    # 3) 静默 → A 带问回来，读回 KV
    time.sleep(args.idle)
    warm = stream_turn(args.base, a, [
        {"role": "user", "content": big},
        {"role": "assistant", "content": a1},
        {"role": "user", "content": ask},
    ], max_tokens=args.max_tokens)
    # 冷轮：全新会话，把整段拼在一个请求里，全量 prefill
    cold = stream_turn(args.base, args.label + "-COLD",
                       [{"role": "user", "content": big + "\n" + ask}], max_tokens=args.max_tokens)

    ok = args.codeword in warm["text"]
    speedup = (cold["ttft_s"] / warm["ttft_s"]) if (warm["ttft_s"] and cold["ttft_s"]) else None
    out = {
        "label": args.label,
        "repeat": args.repeat,
        "warm": {"ttft_s": warm["ttft_s"], "reuse": warm["reuse"],
                 "has_codeword": ok, "text": warm["text"][:160]},
        "cold": {"ttft_s": cold["ttft_s"], "reuse": cold["reuse"],
                 "has_codeword": args.codeword in cold["text"]},
        "ttft_speedup": speedup,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
