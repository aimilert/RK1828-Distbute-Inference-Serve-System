#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KV 换页取证：制造一次 idle 回收（触发 SAVE/LOAD 换页），把盘上的分段文件与
/v1/pool 一起打出来。**必须在板卡上跑**——它要直接列换页目录（如 /dev/shm/rknn-kv）。

为什么单独一个脚本：换页只在"某段对话静默超过 TTL、又有别人非等不可"的那一刻发生，
手工敲请求既难复现又难对齐时间。这里把三步固定下来：

  1. 用对话 A、B 占满全部会话（各建一段上下文，`known` 非空才值得换页）；
  2. 静默超过 CONTEND_IDLE（默认 5s；要触发常规回收可 `--idle` 设到 > IDLE_TTL）；
  3. 再用对话 C 制造排队 —— 让回收按"最闲优先"收掉 A 或 B，回收前会先 `_swap_out`。

然后列出换页目录里的 `conv_*_seg0.kv … _segN.kv` 大小、分组与总量，并抓一次
`/v1/pool` 的换页计数。A3 看"是否恰好 4 段、单段≈每卡每路 KV、总量随上下文近线性"；
A4 用它做对照实验（配合执行器的 `--kv-single-seg`）。

只用标准库：板卡上没有 requests。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time
import urllib.request

MATERIAL = ("边缘推理的多会话调度需要在"
            "算力、显存与公平性之间取平衡。")


def _post(base, path, body, conv=None, timeout=1800):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    if conv is not None:
        req.add_header("X-Conversation-Id", conv)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(base, path, timeout=30):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(base, conv, prompt, max_tokens=8):
    body = {"model": "qwen3.5-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "stream": False}
    return _post(base, "/v1/chat/completions", body, conv=conv)


def dump_swap_dir(swap_dir):
    """按基名（去掉 _segN.kv 后缀）分组，返回每段的字节数。"""
    groups = {}
    for p in sorted(glob.glob(os.path.join(swap_dir, "*"))):
        name = os.path.basename(p)
        m = re.match(r"^(.*)_seg(\d+)\.kv$", name)
        base = m.group(1) if m else name
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        groups.setdefault(base, []).append({"name": name, "bytes": size})
    for base, segs in groups.items():
        segs.sort(key=lambda s: s["name"])
        segs.append({"name": "(total)", "bytes": sum(s["bytes"] for s in segs)})
    return groups


def main():
    ap = argparse.ArgumentParser(description="KV 换页取证（板上运行）")
    ap.add_argument("--base", default="http://127.0.0.1:18280")
    ap.add_argument("--swap-dir", default=os.environ.get("KV_SWAP_DIR", "/dev/shm/rknn-kv"))
    ap.add_argument("--tag", default="probe")
    ap.add_argument("--prompt-chars", type=int, default=0,
                    help="0 = 用内置长材料；>0 = 用该长度的填充文本")
    ap.add_argument("--repeat", type=int, default=150,
                    help="内置材料的重复次数（越大上下文越长）")
    ap.add_argument("--idle", type=float, default=6.0,
                    help="A/B 建完后的静默秒数（> CONTEND_IDLE 即可触发换页）")
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--dump-only", action="store_true",
                    help="不新发请求，只列当前换页目录与 /v1/pool")
    args = ap.parse_args()

    prompt = (("暗号是 ZEBRA-7731。请只回复收到。" + "x" * args.prompt_chars)
              if args.prompt_chars > 0
              else ("请阅读下面这段材料后记住其中的暗号。\n" + MATERIAL * args.repeat))

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    if not args.dump_only:
        a, b = args.tag + "-a", args.tag + "-b"
        chat(args.base, a, prompt, args.max_tokens)
        chat(args.base, b, prompt, args.max_tokens)
        time.sleep(args.idle)
        c = args.tag + "-c"
        chat(args.base, c, "只需回复 OK。", args.max_tokens)

    pool = _get(args.base, "/v1/pool")
    groups = dump_swap_dir(args.swap_dir)
    out = {
        "started": started,
        "base": args.base,
        "swap_dir": args.swap_dir,
        "tag": args.tag,
        "idle": args.idle,
        "prompt_chars": args.prompt_chars,
        "repeat": args.repeat,
        "pool": {k: pool.get(k) for k in
                 ("sessions", "swapped", "swap_bytes", "swap_out", "swap_in",
                  "sessions_busy", "waiting") if k in pool},
        "swap_files": groups,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
