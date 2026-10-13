#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统总容量测试（8K）：连续创建大量对话，统计
  活跃会话（占用卡内 session，bound）+ 冷会话（KV 已换页到 /dev/shm）。

在 **板上**运行（要读换页目录）。每个对话用不同的首条 user 内容，避免互相命中前缀。
判据：创建数 > 容量后，`swapped` 会稳定在"冷容量"（超过 KV_SWAP_CAP / KV_SWAP_MAX_MB /
/dev/shm 余量时按 LRU 淘汰），据此得到 活跃 + 冷 = 总容量。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import time
import urllib.request

MATERIAL = "边缘推理的多会话调度需要在算力、显存与公平性之间取平衡。"


def chat(base, conv, content, max_tokens=8, timeout=1800):
    body = {"model": "qwen3.5-27b", "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(
        base + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Conversation-Id": conv},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def disk_cold(swap_dir):
    groups = set()
    total = 0
    for p in glob.glob(os.path.join(swap_dir, "*")):
        groups.add(os.path.basename(p).rsplit("_seg", 1)[0])
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return len(groups), total


def main():
    ap = argparse.ArgumentParser(description="系统总容量（8K，活跃+冷）")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--swap-dir", default="/dev/shm/rknn-kv")
    ap.add_argument("--label", default="cap")
    ap.add_argument("--conv", type=int, default=25, help="连续创建的对话数")
    ap.add_argument("--repeat", type=int, default=450, help="每条上下文的材料重复次数")
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--max-tokens", type=int, default=8)
    args = ap.parse_args()

    material = MATERIAL * args.repeat
    started = time.time()
    for i in range(args.conv):
        # 每条首行唯一，确保是不同的对话（不同前缀）
        big = "第 %d 段对话。请记住以下材料。\n%s" % (i, material)
        try:
            d = chat(args.base, "%s-%03d" % (args.label, i), big, args.max_tokens)
            perf = d.get("perf", {})
            ctx = perf.get("ctx_tokens")
        except Exception as exc:                                # noqa: BLE001
            ctx = "ERR:%r" % (exc,)
        print("conv %02d ctx=%s elapsed=%.0fs" % (i, ctx, time.time() - started))

    time.sleep(args.settle)
    pool = get(args.base, "/v1/pool")
    slots = pool.get("slots", [])
    active = sum(1 for s in slots if s.get("key") is not None)
    free = sum(1 for s in slots if s.get("key") is None and s.get("state") != "dead")
    dead = sum(1 for s in slots if s.get("state") == "dead")
    cold_pool = pool.get("swapped", 0)
    n_disk, bytes_disk = disk_cold(args.swap_dir)

    out = {
        "label": args.label, "conv_created": args.conv, "repeat": args.repeat,
        "sessions_total": pool.get("sessions"),
        "active": active, "free": free, "dead": dead,
        "cold_pool_swapped": cold_pool,
        "cold_on_disk_groups": n_disk,
        "swap_bytes": bytes_disk,
        "per_cold_mb": round(bytes_disk / n_disk / 1e6, 1) if n_disk else None,
        "total_active_plus_cold_pool": active + cold_pool,
        "total_active_plus_cold_disk": active + n_disk,
        "swap_out_total": pool.get("swap_out_total"),
        "swap_in_total": pool.get("swap_in_total"),
        "last_swap_out_mb_s": pool.get("last_swap_out_mb_s"),
        "last_swap_in_mb_s": pool.get("last_swap_in_mb_s"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
