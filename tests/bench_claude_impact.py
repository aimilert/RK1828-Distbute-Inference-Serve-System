#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_claude_impact.py —— 验证"调度实验设置是否影响 Claude Code 型 Agent"的探针。

与 tools/bench_agent.py 的区别：bench_agent 是"多路 Agent 同时压"的统一尺子；本脚本
针对**单条"主对话"**（模拟 Claude Code 主循环）做可控实验，关键是能：
  · 主对话**回合之间插入思考/工具间隔**（gap）；
  · 在间隔内由**其它对话**制造活动——这是触发网关 `_reap_idle_locked` 的唯一途径
    （代码事实：一段对话自己的会话不会被自己的下一次请求回收，只有**别人**来要会话、
    且它静默超过 IDLE_TTL/CONTEND_IDLE 时才会被收走）；
  · 主对话可选**匿名身份**（不传 conversation_id，复现真实 Claude Code 的行为）。

对每个请求记录：客户端 TTFT/E2E、HTTP 状态、`X-KV-Reuse`（reset/sent/base/full/
swapped/wait）、网关 `perf`（prefill/decode/queue/cached/reset）。**reset=1 是"被回收/
前缀失效 → 全量重算"的铁证**。

只用标准库。运行在本机，压板卡 HTTP。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- 负载文本

_FILLER = [
    "def handle_request(ctx, payload, retries=3):",
    "    for attempt in range(retries):",
    "        result = upstream.call(ctx.session, payload)",
    "        if result.ok: return result",
    "        time.sleep(0.05 * (2 ** attempt))",
    "    raise UpstreamError('exhausted')",
    "class SessionPool:  # 会话池，按身份绑定",
    "    def acquire(self, key): ...",
    "    def release(self, lease, text): ...",
    "# TODO: 把重试策略抽成配置项，别写死在调用点",
]
_SYSTEM = (
    "You are a coding agent running on an edge device. You help the user read, edit and\n"
    "verify code. Prefer minimal, correct changes. Use the provided tools when needed.\n"
    "Working directory: /workspace/rk182X-multisession\n"
)
_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取一个文本文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "把内容写入一个文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]


def filler(nbytes: int, seed: int) -> str:
    out, size, i = [], 0, seed
    while size < nbytes:
        unit = _FILLER[i % len(_FILLER)]
        out.append("%s  # r%d\n" % (unit, i))
        size += len(unit) + 8
        i += 1
    return "".join(out)


def user_turn(tag: str, nbytes: int, seed: int) -> str:
    return "【%s】请阅读下面的代码并给出最小修复：\n" % tag + filler(nbytes, seed)


def sys_prompt(a) -> str:
    """系统提示词。带上 label 作盐：匿名身份是按 system+首条 user 摘要算的，不加盐的话
    两次运行会命中同一段对话、上一轮的会话/排队会污染本轮起点。"""
    return _SYSTEM + ("# run %s\n" % a.label)


# --------------------------------------------------------------------------- HTTP

_KV_RE = re.compile(r"session=(-?\d+); reset=(\d+); sent=(\d+); base=(-?\d+); "
                    r"full=(\d+); swapped=(\d+); wait=([\d.]+)")


def parse_kv(hdr: str):
    m = _KV_RE.search(hdr or "")
    if not m:
        return None
    return {"session": int(m.group(1)), "reset": int(m.group(2)), "sent": int(m.group(3)),
            "base": int(m.group(4)), "full": int(m.group(5)), "swapped": int(m.group(6)),
            "wait": float(m.group(7))}


def stream_request(base: str, body: dict, timeout: float, headers: dict | None = None):
    """发一次流式请求，返回记录 dict。读 X-KV-Reuse 响应头 + 末包 perf。"""
    url = base.rstrip("/") + "/v1/chat/completions"
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=hdr)
    t0 = time.monotonic()
    rec = {"t_start": t0, "ok": False, "status": None, "error": None, "ttft_ms": None,
           "e2e_ms": None, "completion_tokens": 0, "perf": None, "kv": None,
           "finish": None, "text_len": 0}
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        rec["status"] = exc.code
        rec["error"] = exc.read().decode("utf-8", "replace")[:300]
        rec["e2e_ms"] = (time.monotonic() - t0) * 1000.0
        return rec
    except Exception as exc:                                    # noqa: BLE001
        rec["error"] = repr(exc)
        rec["e2e_ms"] = (time.monotonic() - t0) * 1000.0
        return rec
    rec["status"] = 200
    rec["kv"] = parse_kv(resp.headers.get("X-KV-Reuse") or "")
    first = None
    parts = []
    usage = None
    raw_seen = []
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                if len(raw_seen) < 4 and line:
                    raw_seen.append(line)          # 非 SSE 正文（错误 JSON 等）留证
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            if d.get("error"):
                # 网关把"单轮超预算"这类错误发成 HTTP 200 + SSE error 事件（不是 4xx）。
                rec["error"] = json.dumps(d["error"], ensure_ascii=False)[:300]
                rec["sse_error"] = True
                continue
            if d.get("usage"):
                usage = d["usage"]
            if d.get("perf"):
                rec["perf"] = d["perf"]
            ch = (d.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                rec["finish"] = ch["finish_reason"]
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                if first is None:
                    first = time.monotonic()
                parts.append(piece)
    finally:
        try:
            resp.close()
        except Exception:                                       # noqa: BLE001
            pass
    rec["ok"] = not rec.get("sse_error")
    rec["ttft_ms"] = (first - t0) * 1000.0 if first is not None else None
    rec["e2e_ms"] = (time.monotonic() - t0) * 1000.0
    rec["text"] = "".join(parts)
    rec["text_len"] = len(rec["text"])
    if not rec["text"] and usage is None and raw_seen:
        rec["error"] = " | ".join(raw_seen)[:300]
    if usage and isinstance(usage.get("completion_tokens"), int):
        rec["completion_tokens"] = usage["completion_tokens"]
    return rec


# --------------------------------------------------------------------------- 运行

class Run:
    def __init__(self, args):
        self.a = args
        self.base = args.base
        self.records = []
        self.lock = threading.Lock()
        self.main_headers = ({"X-Priority": str(args.priority)}
                             if args.priority is not None else None)
        # 主对话进入第 k 个 gap 时 set 的事件——triggers 用它保证落进 gap 里
        self.gap_events = [threading.Event() for _ in range(max(1, args.rounds))]

    def add(self, rec):
        with self.lock:
            self.records.append(rec)

    def chat(self, msgs, conv_id, anon, phase, out_tokens, headers=None):
        a = self.a
        body = {"model": a.model, "messages": msgs, "stream": True,
                "max_tokens": out_tokens, "stream_options": {"include_usage": True},
                "chat_template_kw": {"enable_thinking": a.think}}
        if conv_id and not anon:
            body["conversation_id"] = conv_id
        if a.tools:
            body["tools"] = _TOOLS
        rec = stream_request(self.base, body, a.timeout, headers)
        rec["phase"] = phase
        rec["conv"] = conv_id or "(anon)"
        self.add(rec)
        return rec


def run_main(run):
    a = run.a
    conv = a.conv_prefix + "-main"
    hist = [{"role": "system", "content": sys_prompt(a)}]
    for r in range(a.rounds):
        hist.append({"role": "user",
                     "content": user_turn("主对话第%d轮" % (r + 1), a.prompt_bytes, r)})
        rec = run.chat(list(hist), conv, a.anon, "main-r%d" % r, a.output_tokens,
                       headers=run.main_headers)
        rec["round"] = r
        if rec["ok"] and rec.get("text"):
            # 必须把**真实生成文本**追加进历史：网关的前缀判据要求"本轮 prompt 是上一轮
            # prompt + 上一轮真实输出"的严格前缀，占位符会让每轮 reset 全量重算。
            hist.append({"role": "assistant", "content": rec["text"]})
        if r < a.rounds - 1 and a.gap > 0:
            run.gap_events[r].set()
            time.sleep(a.gap)


def run_trigger(run, idx):
    a = run.a
    if 0 <= a.trigger_after_round < len(run.gap_events):
        run.gap_events[a.trigger_after_round].wait(timeout=a.timeout)
    if a.trigger_delay > 0:
        time.sleep(a.trigger_delay)
    conv = a.conv_prefix + "-trig%d" % idx
    hist = [{"role": "system", "content": sys_prompt(a)},
            {"role": "user", "content": user_turn("触发%d" % idx, a.trigger_bytes, 900 + idx)}]
    run.chat(hist, conv, False, "trig%d" % idx, a.trigger_tokens)


def run_holder(run, idx):
    a = run.a
    conv = a.conv_prefix + "-hold%d" % idx
    hist = [{"role": "system", "content": sys_prompt(a)},
            {"role": "user", "content": user_turn("占位%d" % idx, a.hold_bytes, 700 + idx)}]
    run.chat(hist, conv, False, "hold%d" % idx, a.hold_tokens)


def main():
    ap = argparse.ArgumentParser(description="Claude Code 型调度影响探针")
    ap.add_argument("--base", default="http://192.168.0.184:18280")
    ap.add_argument("--model", default="qwen3.5-27b")
    ap.add_argument("--label", default="run")
    ap.add_argument("--conv-prefix", default="claude")
    ap.add_argument("--anon", action="store_true", help="主对话不传 id（复现真实 Claude）")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--gap", type=float, default=0.0, help="主对话回合间思考/工具间隔(秒)")
    ap.add_argument("--prompt-bytes", type=int, default=3000)
    ap.add_argument("--output-tokens", type=int, default=64)
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--triggers", type=int, default=0, help="触发对话数（在 gap 内各发一次）")
    ap.add_argument("--trigger-after-round", type=int, default=0,
                    help="triggers 在主对话第几轮后的 gap 触发")
    ap.add_argument("--trigger-delay", type=float, default=0.0,
                    help="gap 开始后再等多少秒才发触发请求（要让主对话静默超过 TTL）")
    ap.add_argument("--trigger-bytes", type=int, default=400)
    ap.add_argument("--trigger-tokens", type=int, default=16)
    ap.add_argument("--holders", type=int, default=0, help="占位对话数（t0 起各发一次长请求）")
    ap.add_argument("--hold-bytes", type=int, default=400)
    ap.add_argument("--hold-tokens", type=int, default=400)
    ap.add_argument("--priority", type=int, default=None, help="主对话 X-Priority")
    ap.add_argument("--co-run", default="", help="并发实验负载命令行（本机执行，如 bench_agent）")
    ap.add_argument("--keep-conversations", action="store_true",
                    help="跑完不交还会话（默认交还；不交还会污染下一次运行的起点）")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "perf_results", "claude_impact"))
    args = ap.parse_args()

    try:
        urllib.request.urlopen(args.base + "/v1/perf/reset", data=b"{}", timeout=8).read()
    except Exception as exc:                                    # noqa: BLE001
        print("warn: /v1/perf/reset 不可用：%r" % exc, file=sys.stderr)

    run = Run(args)
    threads = []
    co_proc = None
    t0 = time.monotonic()
    if args.co_run:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        co_proc = subprocess.Popen(args.co_run, shell=True, cwd=repo_root)
    for h in range(args.holders):
        th = threading.Thread(target=run_holder, args=(run, h), daemon=True)
        th.start()
        threads.append(th)
    if args.holders > 0:
        # 等占位对话真的把会话拿到手，再让主对话进场——否则是主对话抢到会话、占位去排队，
        # 方向正好反过来（第一版 H3 就踩了这个竞态）。
        # /v1/pool 没有 sessions_bound 这个键，得数 slots 里 conversation 非空的。
        t_wait = time.monotonic()
        while time.monotonic() - t_wait < 60:
            try:
                p = json.load(urllib.request.urlopen(args.base + "/v1/pool", timeout=5))
                bound = sum(1 for s in (p.get("slots") or []) if s.get("conversation"))
                if bound >= args.holders:
                    break
            except Exception:                                   # noqa: BLE001
                pass
            time.sleep(0.2)
    for t in range(args.triggers):
        th = threading.Thread(target=run_trigger, args=(run, t), daemon=True)
        th.start()
        threads.append(th)

    run_main(run)
    for th in threads:
        th.join()
    if co_proc:
        co_proc.wait()
    wall = time.monotonic() - t0

    # 主动交还会话：不交还的话它要占到 IDLE_TTL，下一次运行的起点会被上一轮的会话/排队污染。
    if not args.keep_conversations:
        ids = []
        if not args.anon:
            ids.append(args.conv_prefix + "-main")
        ids += [args.conv_prefix + "-trig%d" % i for i in range(args.triggers)]
        ids += [args.conv_prefix + "-hold%d" % i for i in range(args.holders)]
        for cid in ids:
            try:
                req = urllib.request.Request(
                    args.base + "/v1/conversations/close",
                    data=json.dumps({"conversation_id": cid}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=8).read()
            except Exception:                                   # noqa: BLE001
                pass

    try:
        gw_perf = json.load(urllib.request.urlopen(args.base + "/v1/perf", timeout=8))
    except Exception:                                           # noqa: BLE001
        gw_perf = {}
    try:
        gw_pool = json.load(urllib.request.urlopen(args.base + "/v1/pool", timeout=8))
    except Exception:                                           # noqa: BLE001
        gw_pool = {}

    result = {"label": args.label, "argv": sys.argv[1:], "wall_s": round(wall, 3),
              "config": vars(args), "gateway_perf": gw_perf, "gateway_pool": gw_pool,
              "records": [{k: v for k, v in r.items() if k != "text"}
                          for r in run.records]}
    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.out, "H_%s_%s.json" % (args.label, stamp))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print("\n===== bench_claude_impact: %s =====" % args.label)
    print("wall %.1fs  records=%d" % (wall, len(run.records)))
    for r in run.records:
        p = r.get("perf") or {}
        kv = r.get("kv") or {}
        reset = p.get("reset", kv.get("reset"))
        print("  %-12s ok=%-5s status=%-4s reset=%-5s pf_tok=%-6s pf_ms=%-9s "
              "cached=%-5s ttft=%-9s e2e=%-9s wait=%s"
              % (r.get("phase"), r.get("ok"), r.get("status"), reset,
                 p.get("prefill_tokens"), p.get("prefill_ms"), p.get("cached_tokens"),
                 None if r.get("ttft_ms") is None else round(r["ttft_ms"], 1),
                 None if r.get("e2e_ms") is None else round(r["e2e_ms"], 1),
                 kv.get("wait")))
    print("perf: %s" % json.dumps(gw_perf.get("kv", {}), ensure_ascii=False))
    print("saved: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
