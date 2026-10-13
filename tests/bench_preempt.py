#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抢占代价实测：被 STOP（中止）的对话，下一轮是否丢掉 KV 复用（全量重算）？

背景：本网关**不抢占**（见 acquire 注释与 release 记账）：一轮是原子的，中途 STOP 会给客户端
一个被截断的答复，而 `known` 按"已生成部分"写回，于是被抢的对话下一轮前缀对不上 → `reset=1`
全量重算。本脚本量化这个代价，作为"抢占式 SRPT 是否可行"的依据。

对照：
  · A（被抢占）：第 1 轮流式长生成，收到若干块后 STOP（模拟抢占）；第 2 轮重发同一请求。
  · B（对照）：第 1 轮跑完；第 2 轮正常追问（应命中复用）。
比较两段第 2 轮的 prefill_ms / reset。只用标准库。
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

MATERIAL = "边缘算力平台的调度优化需要在算力显存与公平性之间取得平衡。"


def _req(base, path, body, conv=None, stream=False):
    h = {"Content-Type": "application/json"}
    if conv is not None:
        h["X-Conversation-Id"] = conv
    if stream:
        h["Accept"] = "text/event-stream"
    return urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                  headers=h, method="POST")


def stream_turn(base, conv, messages, max_tokens, stop_after_deltas=None):
    body = {"model": "qwen3.5-27b", "messages": messages, "max_tokens": max_tokens, "stream": True}
    parts, rid, deltas, stopped = [], None, 0, False
    with urllib.request.urlopen(_req(base, "/v1/chat/completions", body, conv, True),
                                timeout=1200) as r:
        rid = r.headers.get("X-Request-Id")
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
                parts.append(delta)
                deltas += 1
                if stop_after_deltas and deltas >= stop_after_deltas and not stopped:
                    stopped = True
                    # 同线程内发 STOP：服务端是 ThreadingHTTPServer，会并发处理这一 POST。
                    with urllib.request.urlopen(
                            _req(base, "/v1/stop", {"request_id": int(rid)}), timeout=30) as sr:
                        sr.read()
    return {"rid": rid, "text": "".join(parts), "deltas": deltas, "stopped": stopped}


def full_turn(base, conv, messages, max_tokens):
    body = {"model": "qwen3.5-27b", "messages": messages, "max_tokens": max_tokens, "stream": False}
    with urllib.request.urlopen(_req(base, "/v1/chat/completions", body, conv, False),
                                timeout=1200) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="抢占代价实测")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--repeat", type=int, default=100)
    ap.add_argument("--stop-after", type=int, default=12, help="收到多少块后发 STOP")
    ap.add_argument("--max-tokens", type=int, default=200)
    args = ap.parse_args()

    big = "请阅读并记住材料。\n" + MATERIAL * args.repeat
    follow = "请只回答：好的"

    # A：被抢占
    a1 = stream_turn(args.base, "preempt-A", [{"role": "user", "content": big}],
                     args.max_tokens, stop_after_deltas=args.stop_after)
    a2 = full_turn(args.base, "preempt-A", [{"role": "user", "content": big}], 8)
    pa = a2.get("perf", {})

    # B：对照（正常两轮）
    b1 = stream_turn(args.base, "preempt-B", [{"role": "user", "content": big}], args.max_tokens)
    b2 = full_turn(args.base, "preempt-B", [
        {"role": "user", "content": big},
        {"role": "assistant", "content": b1["text"]},
        {"role": "user", "content": follow},
    ], 8)
    pb = b2.get("perf", {})

    out = {
        "A_preempt": {"turn1_deltas": a1["deltas"], "stopped": a1["stopped"],
                      "turn2_prefill_ms": pa.get("prefill_ms"),
                      "turn2_prefill_tokens": pa.get("prefill_tokens"),
                      "turn2_reset": pa.get("reset"), "turn2_ctx": pa.get("ctx_tokens")},
        "B_control": {"turn1_deltas": b1["deltas"],
                      "turn2_prefill_ms": pb.get("prefill_ms"),
                      "turn2_prefill_tokens": pb.get("prefill_tokens"),
                      "turn2_reset": pb.get("reset"), "turn2_ctx": pb.get("ctx_tokens")},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
