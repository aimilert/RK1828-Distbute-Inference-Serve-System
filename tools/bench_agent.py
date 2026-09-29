#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_agent.py —— 端侧"Claude Agent 型"负载基准（调度算法 A/B 的统一尺子）。

为什么需要它：要把服务器上的调度算法（见 algorithm/mlfq/）移植到板卡网关，必须先有
一个**可重复、可对比**的测量口径。这个脚本用与 Claude Code 同构的请求形态压板卡：
  · 多个会话（session）各自是一条**严格串行的回合链**（下一轮必须等上一轮 + 工具等待结束）；
  · 同一会话的相邻回合共享前缀（messages 只追加），触发网关的粘性 KV 复用；
  · 回合之间插入"工具等待"（Agent 真实存在的长尾，期间别的会话可以补位）；
  · 全程流式（Claude Code 就是流式），每轮带 stream_options.include_usage。

指标分两层，**两层都要看**：
  客户端测：TTFT / E2E / makespan / 总吞吐        —— 用户真实体感
  网关 /v1/perf：prefill/decode 速率、排队、KV 命中、气泡比 —— 调度算法的作用点
（客户端 TTFT 含排队与网络；网关 TTFT 不含排队。两者相减就是"排队+传输"的开销。）

输出：
  · 控制台摘要表
  · <out>/bench_<label>_<ts>.json（全量明细，供离线画图/回归）
  · <out>/bench_results.csv（追加一行；列名与 algorithm/mlfq/analyze.py 兼容）
  · --compare a.json b.json：打印两次运行的差异（A/B 一眼看结论）

用法（板卡上，网关已起）：
    python3 bench_agent.py --base http://127.0.0.1:18280 --label baseline
    python3 bench_agent.py --base http://127.0.0.1:18280 --label mlfq --sessions 2
    python3 bench_agent.py --compare perf_results/bench_baseline_x.json \
                                   perf_results/bench_mlfq_y.json

注意：本脚本只依赖 Python 3 标准库（与仓库其它脚本一致），不需要 requests。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------- 负载构造

# 一段"像代码"的填充文本：真实 Agent 的上下文里大量是代码/文件内容，用重复文本容易被
# 模型的 tokenizer 压成很低的 token 数，量出来的 prefill 会偏轻。这里用不规则片段拼。
_FILLER_UNITS = [
    "def handle_request(ctx, payload, retries=3):",
    "    for attempt in range(retries):",
    "        result = upstream.call(ctx.session, payload)",
    "        if result.ok: return result",
    "        time.sleep(0.05 * (2 ** attempt))",
    "    raise UpstreamError('exhausted')",
    "# TODO: 把重试策略抽成配置项，别写死在调用点",
    "class SessionPool:  # 会话池，按身份绑定",
    "    def acquire(self, key): ...",
    "    def release(self, lease, text): ...",
]
_SYSTEM = (
    "You are a coding agent running on an edge device. You help the user read, edit and\n"
    "verify code. Prefer minimal, correct changes. Use the provided tools when needed.\n"
    "Working directory: /workspace/rk182X-multisession\n"
)

# `--hetero`：让各会话的负载**不一样**。为什么需要它：MLFQ 的五个因子都是"相对大小"参与
# 打分，若所有会话的 prompt/输出/工具等待完全相同（同构负载），归一化后各因子的区分度为零，
# 打分排序会退化成 FCFS——这不是算法没用，是负载没有可调度的差异。真实 Claude 会话本来
# 就有长有短（主对话长、后台任务短），所以 A/B 必须跑异构负载才看得出区别。
# 下标即会话序号，倍数作用于该会话每轮的 prompt 增量、max_tokens 与工具等待。
HETERO_SCALE = (2.0, 0.5, 1.5, 0.75, 1.25, 0.4)
_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取一个文本文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "grep", "description": "在仓库里搜索一个正则",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "把文件里的一段文本替换成另一段",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                       "old": {"type": "string"},
                                                       "new": {"type": "string"}},
                       "required": ["path", "old", "new"]}}},
]


def make_filler(nbytes: int, seed: int) -> str:
    """生成约 nbytes 字节的填充文本（确定性：同一 seed 每次一样，保证可重复）。"""
    out = []
    size = 0
    i = seed
    while size < nbytes:
        unit = _FILLER_UNITS[i % len(_FILLER_UNITS)]
        out.append("%s  # r%d\n" % (unit, i))
        size += len(unit) + 8
        i += 1
    return "".join(out)


def build_task(session_idx: int, round_idx: int, prompt_bytes: int) -> str:
    """这一轮的新增用户输入（确定性：同一 seed 每次一样，保证可重复）。"""
    return (
        "第 %d 轮：请阅读第 %d 段代码，找出其中的 bug 并给出最小修复。\n"
        % (round_idx + 1, session_idx + 1)
        + make_filler(prompt_bytes, seed=session_idx * 100 + round_idx)
    )


# --------------------------------------------------------------------------- HTTP / SSE

def stream_request(base: str, body: dict, timeout: float):
    """发一次流式请求，返回 (结果 dict)。边读边记 TTFT，直到 [DONE]。

    返回字段：
      ok / status / error
      ttft_ms        客户端首 token（首个非空 delta.content 到达）——含排队与网络
      e2e_ms        起请求 → [DONE]
      text          正文（用于拼下一轮历史）
      completion_tokens / usage
      perf          网关在最后一个 usage chunk 里带的 per-request 明细（可能为 None）
    """
    url = base.rstrip("/") + "/v1/chat/completions"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    first = None
    text_parts = []
    usage = None
    perf = None
    finish = None
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", "replace")[:200]
        return {"ok": False, "status": exc.code, "error": body_txt, "ttft_ms": None,
                "e2e_ms": (time.monotonic() - t0) * 1000.0, "text": "",
                "completion_tokens": 0, "usage": None, "perf": None, "finish": None}
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "status": None, "error": repr(exc), "ttft_ms": None,
                "e2e_ms": (time.monotonic() - t0) * 1000.0, "text": "",
                "completion_tokens": 0, "usage": None, "perf": None, "finish": None}
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            if d.get("usage"):
                usage = d["usage"]
            if d.get("perf"):
                perf = d["perf"]
            ch = (d.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                if first is None:
                    first = time.monotonic()
                text_parts.append(piece)
    finally:
        try:
            resp.close()
        except Exception:                                       # noqa: BLE001
            pass
    tok = 0
    if usage and isinstance(usage.get("completion_tokens"), int):
        tok = usage["completion_tokens"]
    return {"ok": True, "status": 200, "error": None,
            "ttft_ms": (first - t0) * 1000.0 if first is not None else None,
            "e2e_ms": (time.monotonic() - t0) * 1000.0,
            "text": "".join(text_parts), "completion_tokens": tok,
            "usage": usage, "perf": perf, "finish": finish}


def post_json(base: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))


def get_json(base: str, path: str) -> dict:
    return json.loads(urllib.request.urlopen(base.rstrip("/") + path,
                                             timeout=15).read().decode("utf-8"))


# --------------------------------------------------------------------------- 会话线程

class SessionRunner(threading.Thread):
    def __init__(self, idx, args, out_records, lock):
        super().__init__(daemon=True)
        self.idx = idx
        self.args = args
        self.out = out_records
        self.lock = lock
        self.history = []
        self.records = []
        # 异构负载：每个会话一个固定倍数（同构时全是 1.0）。倍数在会话级固定，这样
        # "某一路一直是长任务"这件事在整个运行里稳定，调度算法才有稳定的差异可用。
        self.scale = HETERO_SCALE[idx % len(HETERO_SCALE)] if args.hetero else 1.0

    def run(self):
        a = self.args
        conv = "%s-s%d" % (a.label, self.idx)
        prompt_bytes = max(200, int(a.prompt_bytes * self.scale))
        output_tokens = max(8, int(a.output_tokens * self.scale))
        tool_wait = a.tool_wait * self.scale
        if a.stagger > 0:
            time.sleep(self.idx * a.stagger)
        try:
            for r in range(a.rounds):
                # 历史必须是**完整对话**（user 与 assistant 都追加），否则本轮 prompt 不是
                # 上一轮 prompt 的前缀——网关的前缀判据直接失败、每轮 reset 全量重算，
                # 量出来的"KV 命中率"永远是 0（第一版 bench 就踩了这个坑：只追加了 assistant）。
                user_msg = {"role": "user",
                            "content": build_task(self.idx, r, prompt_bytes)}
                msgs = ([{"role": "system", "content": _SYSTEM}]
                        + self.history + [user_msg])
                body = {
                    "model": a.model,
                    "messages": msgs,
                    "stream": True,
                    "max_tokens": output_tokens,
                    "stream_options": {"include_usage": True},
                    "conversation_id": conv,
                    "chat_template_kw": {"enable_thinking": a.think},
                }
                if a.tools:
                    body["tools"] = _TOOLS
                res = stream_request(a.base, body, a.timeout)
                res["session"] = self.idx
                res["round"] = r
                res["conversation"] = conv
                res["prompt_bytes"] = len(json.dumps(msgs).encode("utf-8"))
                res["scale"] = self.scale
                res["max_tokens"] = output_tokens
                self.records.append(res)
                # 这一轮的 user 与 assistant 都进历史：下一轮就是"同一段历史 + 新问题"，
                # 前缀逐字节一致，网关才会只发差异部分（粘性复用）。
                self.history.append(user_msg)
                if res["ok"] and res["text"]:
                    self.history.append({"role": "assistant", "content": res["text"]})
                # 工具等待：Agent 回合之间真实存在的空档（期间别的会话可以补位）。
                if r < a.rounds - 1 and tool_wait > 0:
                    time.sleep(tool_wait)
        finally:
            # 跑完主动交还会话（除非 --keep-conversations）。不交还的话它要占到 IDLE_TTL，
            # **下一次 bench 开局就得排队等回收**（CONTEND_IDLE 15s），连跑两轮的 makespan
            # / TTFT p99 会被这段等待污染——A/B 对比最忌讳这个。
            if not a.keep_conversations:
                try:
                    post_json(a.base, "/v1/conversations/close", {"conversation_id": conv})
                except Exception:                               # noqa: BLE001
                    pass
            with self.lock:
                self.out.extend(self.records)


# --------------------------------------------------------------------------- 统计

def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    idx = max(0, min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1)))))
    return round(vals[idx], 2)


def _gw_field(rec, *path, default=None):
    """从网关 perf 里取字段（rec['perf'] 可能为 None，旧网关没有这个扩展）。"""
    cur = rec.get("perf")
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def summarize(result):
    """把一轮运行的所有记录聚合成一份可对比的报告。"""
    recs = result["records"]
    ok = [r for r in recs if r["ok"]]
    ttft = [r["ttft_ms"] for r in ok]
    e2e = [r["e2e_ms"] for r in ok]
    gw_ttft = [_gw_field(r, "ttft_ms") for r in ok]
    gw_wait = [_gw_field(r, "queue_wait_s") for r in recs]
    pf_rate = [_gw_field(r, "prefill_tok_s") for r in ok]
    dc_rate = [_gw_field(r, "decode_tok_s") for r in ok]
    tpot = [_gw_field(r, "tpot_ms") for r in ok]
    pf_tok = sum(_gw_field(r, "prefill_tokens", default=0) or 0 for r in ok)
    pf_ms = sum(_gw_field(r, "prefill_ms", default=0.0) or 0.0 for r in ok)
    dc_tok = sum(_gw_field(r, "decode_tokens", default=r["completion_tokens"]) or 0
                 for r in ok)
    dc_ms = sum(_gw_field(r, "decode_ms", default=0.0) or 0.0 for r in ok)
    tok = sum(r["completion_tokens"] for r in ok)
    starts = [r["t0"] for r in recs]
    ends = [r["t1"] for r in recs]
    makespan = (max(ends) - min(starts)) if recs else 0.0
    # 「各路速率和」= 每一路用**自己的端到端窗口**算的 tok/s 再加起来——这与演示网页上
    # 那个数是同一个口径（网页的 `r.rate = tok / el`，再相加），它**不含排队**、只把
    # prefill 与解码算进那一路自己的窗口。头条那个 `output_tokens_s` 则是
    # 总 token ÷ 整个 makespan，**把排队和别人的尾巴都摊进来了**。两个都不是错的，
    # 但回答的问题不同：前者是"并发起来时能跑多快"，后者是"这批活干完平均多快"。
    per_lane_sum = 0.0
    for r in ok:
        if r.get("e2e_ms"):
            per_lane_sum += r["completion_tokens"] / (r["e2e_ms"] / 1000.0)
    # 客户端侧会话忙时：把每个会话的 e2e 串起来（含排队）。工具等待不算忙——那正是"补位"的窗口。
    busy = 0.0
    for s in range(result["sessions"]):
        busy += sum(r["e2e_ms"] for r in ok if r["session"] == s) / 1000.0
    ratio = min(1.0, busy / (result["sessions"] * makespan)) if makespan > 0 else None
    gw_slots = result.get("gateway_perf", {}).get("slots", {}) or {}
    return {
        "label": result["label"],
        "sessions": result["sessions"],
        "rounds": result["rounds"],
        "prompt_bytes": result["prompt_bytes"],
        "output_tokens": result["output_tokens"],
        "tool_wait": result["tool_wait"],
        "tools": result["tools"],
        "n": len(recs), "ok": len(ok),
        "errors": len(recs) - len(ok),
        # —— 调度算法要对比的核心指标（列名与 algorithm/mlfq/analyze.py 对齐）——
        "makespan": round(makespan, 3),
        "e2e_p50": pct(e2e, 50), "e2e_p99": pct(e2e, 99),
        "ttft_p50": pct(ttft, 50), "ttft_p99": pct(ttft, 99),
        "output_tokens_s": round(tok / makespan, 2) if makespan > 0 else None,
        "per_lane_sum_tok_s": round(per_lane_sum, 2),
        "bubble_ratio": round(max(0.0, 1.0 - ratio), 4) if ratio is not None else None,
        # —— 分阶段的细账（prefill/decode 速率与排队，来自网关 DONE 帧）——
        "gw_ttft_p50": pct(gw_ttft, 50), "gw_ttft_p99": pct(gw_ttft, 99),
        "queue_wait_p50": pct(gw_wait, 50), "queue_wait_p99": pct(gw_wait, 99),
        "prefill_tok_s": round(pf_tok / (pf_ms / 1000.0), 1) if pf_ms > 0 else None,
        "prefill_rate_p50": pct(pf_rate, 50),
        "decode_tok_s": round(dc_tok / (dc_ms / 1000.0), 2) if dc_ms > 0 else None,
        "decode_rate_p50": pct(dc_rate, 50),
        "tpot_ms_p50": pct(tpot, 50),
        "tokens": {"prefill": pf_tok, "decode": dc_tok},
        "gateway_slots": gw_slots or None,
    }


# --------------------------------------------------------------------------- 对比

BETTER = {"makespan": -1, "e2e_p50": -1, "e2e_p99": -1, "ttft_p50": -1, "ttft_p99": -1,
          "queue_wait_p50": -1, "queue_wait_p99": -1, "tpot_ms_p50": -1,
          "bubble_ratio": -1, "output_tokens_s": 1, "prefill_tok_s": 1, "decode_tok_s": 1}


def compare(a, b):
    keys = [k for k in BETTER if k in a and k in b]
    print("指标                     A(%s)            B(%s)            变化     结论" %
          (a.get("label", "A"), b.get("label", "B")))
    print("-" * 88)
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            continue
        delta = vb - va
        rel = (delta / abs(va) * 100.0) if va else None
        good = (BETTER[k] * delta) > 0
        tag = "更好" if good else ("更差" if (BETTER[k] * delta) < 0 else "持平")
        rel_s = ("%+.1f%%" % rel) if rel is not None else "n/a"
        print("%-22s %-16s %-16s %-8s %s" % (k, round(va, 3), round(vb, 3), rel_s, tag))


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="端侧 Claude Agent 型负载基准")
    ap.add_argument("--base", default="http://127.0.0.1:18280", help="网关地址")
    ap.add_argument("--model", default="qwen3.5-27b")
    ap.add_argument("--label", default="run", help="这一轮的名字（baseline/mlfq/...）")
    ap.add_argument("--sessions", type=int, default=2, help="并发会话数（= Agent 并行对话数）")
    ap.add_argument("--rounds", type=int, default=3, help="每个会话的回合数")
    ap.add_argument("--prompt-bytes", type=int, default=2000, help="每轮新增长度（字节）")
    ap.add_argument("--output-tokens", type=int, default=96, help="每轮 max_tokens")
    ap.add_argument("--tool-wait", type=float, default=0.5, help="回合间的工具等待（秒）")
    ap.add_argument("--stagger", type=float, default=0.0, help="会话启动错峰（秒）")
    ap.add_argument("--tools", action="store_true", help="注入 3 个工具定义")
    ap.add_argument("--hetero", action="store_true",
                    help="异构负载：各会话的 prompt/输出/工具等待按 HETERO_SCALE 不同，"
                         "调度算法才有可分辨的差异（同构负载下打分退化为 FCFS）")
    ap.add_argument("--think", action="store_true", help="开启思考（默认关）")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "perf_results"))
    ap.add_argument("--no-reset", action="store_true", help="跑前不 reset 网关性能窗口")
    ap.add_argument("--keep-conversations", action="store_true",
                    help="跑完不 close 会话（默认会 close；保留用于测回收路径）")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                    help="只做对比，不打负载")
    args = ap.parse_args()

    if args.compare:
        with open(args.compare[0], encoding="utf-8") as fh:
            a = json.load(fh)
        with open(args.compare[1], encoding="utf-8") as fh:
            b = json.load(fh)
        compare(a.get("summary", a), b.get("summary", b))
        return 0

    # 起跑线：清空网关性能窗口，跑完读到的就纯粹是本轮负载。
    if not args.no_reset:
        try:
            post_json(args.base, "/v1/perf/reset")
        except Exception as exc:                                # noqa: BLE001
            print("warn: /v1/perf/reset 不可用（旧网关？）：%r" % (exc,), file=sys.stderr)

    records = []
    lock = threading.Lock()
    runners = [SessionRunner(i, args, records, lock) for i in range(args.sessions)]
    wall0 = time.monotonic()
    for r in runners:
        r.start()
    for r in runners:
        r.join()
    wall1 = time.monotonic()
    # 为 makespan/气泡比拼一条时间轴：每个会话的回合是**严格串行**的（工具等待也算在内），
    # 所以按会话、按轮号累加 e2e 就是每轮的绝对起止。会话之间用 stagger 错峰。
    t_cursor = {}
    for s in range(args.sessions):
        cur = wall0 + s * args.stagger
        for r in sorted([x for x in records if x["session"] == s], key=lambda x: x["round"]):
            r["t0"] = cur
            cur += r["e2e_ms"] / 1000.0
            r["t1"] = cur
            if r["round"] < args.rounds - 1:
                cur += args.tool_wait

    gateway_perf = {}
    try:
        gateway_perf = get_json(args.base, "/v1/perf")
    except Exception as exc:                                    # noqa: BLE001
        print("warn: /v1/perf 不可用（旧网关？）：%r" % (exc,), file=sys.stderr)

    result = {
        "label": args.label, "sessions": args.sessions, "rounds": args.rounds,
        "prompt_bytes": args.prompt_bytes, "output_tokens": args.output_tokens,
        "tool_wait": args.tool_wait, "tools": bool(args.tools), "think": bool(args.think),
        "hetero": bool(args.hetero),
        "base": args.base, "model": args.model, "wall_s": round(wall1 - wall0, 3),
        "gateway_perf": gateway_perf,
        "records": [
            {k: v for k, v in r.items() if k != "text"} | {"text_len": len(r.get("text") or "")}
            for r in records
        ],
    }
    summary = summarize(result)
    result["summary"] = summary

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(args.out, "bench_%s_%s.json" % (args.label, stamp))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    csv_path = os.path.join(args.out, "bench_results.csv")
    cols = ["policy", "makespan", "e2e_p50", "e2e_p99", "ttft_p99", "output_tokens_s",
            "bubble_ratio", "weights", "sessions", "rounds", "prompt_bytes",
            "output_tokens", "tool_wait", "tools", "ttft_p50", "queue_wait_p50",
            "queue_wait_p99", "prefill_tok_s", "decode_tok_s", "tpot_ms_p50", "ok", "n"]
    new = not os.path.exists(csv_path)
    with open(csv_path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(",".join(cols) + "\n")
        row = []
        for c in cols:
            v = summary.get("label") if c == "policy" else summary.get(c)
            if c == "weights":
                v = "[]"
            row.append("" if v is None else str(v))
        fh.write(",".join(row) + "\n")

    # ---- 控制台摘要 ----
    print("\n===== bench_agent: %s =====" % args.label)
    print("负载: %d 会话 × %d 轮, 每轮 +%d 字节, max_tokens=%d, 工具等待 %.2fs, tools=%s"
          % (args.sessions, args.rounds, args.prompt_bytes, args.output_tokens,
             args.tool_wait, bool(args.tools)))
    print("成功 %d/%d, wall %.2fs, makespan %.2fs"
          % (summary["ok"], summary["n"], result["wall_s"], summary["makespan"]))
    print("客户端: TTFT p50/p99 = %s / %s ms | E2E p50/p99 = %s / %s ms"
          % (summary["ttft_p50"], summary["ttft_p99"], summary["e2e_p50"], summary["e2e_p99"]))
    print("网关  : TTFT p50/p99 = %s / %s ms | 排队 p50/p99 = %s / %s s"
          % (summary["gw_ttft_p50"], summary["gw_ttft_p99"],
             summary["queue_wait_p50"], summary["queue_wait_p99"]))
    print("速率  : prefill %s tok/s (p50 %s) | decode %s tok/s (p50 %s) | TPOT %s ms"
          % (summary["prefill_tok_s"], summary["prefill_rate_p50"],
             summary["decode_tok_s"], summary["decode_rate_p50"], summary["tpot_ms_p50"]))
    gw_slots = summary.get("gateway_slots") or {}
    print("吞吐  : 总输出 %s tok/s（总 token ÷ 墙钟，含排队与 prefill）| "
          "各路速率和 %s tok/s（与演示网页同口径，不含排队）"
          % (summary["output_tokens_s"], summary["per_lane_sum_tok_s"]))
    print("气泡比: 客户端 %s / 网关 %s"
          % (summary["bubble_ratio"], gw_slots.get("bubble_ratio")))
    print("KV    : 命中率 %s" % (gateway_perf.get("kv", {}) or {}).get("cache_hit_ratio"))
    print("结果  : %s" % json_path)
    print("      : %s（可直接喂 algorithm/mlfq/analyze.py）" % csv_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
