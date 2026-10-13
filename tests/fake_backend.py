#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rkllm_multicard_demo --serve 的桩后端：只讲帧协议，不做推理。

存在的理由：板卡上一次模型加载串行 ~140s（`PARALLEL_LOAD=1` 时 ~62s），而网关这半边（分帧、增量 UTF-8 解码、
会话池、模板渲染、并发写不交错）全都是纯 Python 逻辑，本地就能验。把桩后端接在
网关后面跑 `--selftest`，就能在**没有任何板卡参与**的情况下把这一半清零，剩下的
问题必然出在 C++ 侧或硬件上。

它刻意比真后端更"刻薄"：
  · DELTA 按固定字节数切，**故意把一个多字节字符切成两半**投递——网关必须用增量
    解码器才能拼回原字，逐帧 decode 会吐出替换字符；
  · 每个请求在独立线程里回答（带一点延迟），这样 N 路并发会同时往 stdout 写帧，
    网关那边如果没把 stdin 的帧写整体加锁，这里的解析器立刻会解析失败；
  · 头行解析不了一律 rc=2 退出，让失败显式暴露，而不是静默丢帧。

它也照抄了 `STAT` 资源帧（2026-09-20 加的）：真后端在**输入线程上就地**答一包 JSON，
桩就用合成数字答同一包——这样网关的 `CardStats`、`/v1/system` 和网页那块资源面板
在没有板卡的时候也能端到端跑起来。**合成数字是编的**，但它按同一本账算（1078 MB/卡
可分配 ÷ 单价 × 路数），好让页面上的进度条看着是对的。

用法（由网关自动拉起，一般不手跑）：
    python3 fake_backend.py --sessions 2 [--serve-fd N] [--think-prefix] [--tool-call]
"""
import argparse
import json
import os
import sys
import threading
import time

REPLY = ("你好，我是一个跑在 4 片 RK1828 上的 Qwen3.5-27B，"
         "由多会话并发执行器驱动。这句话里混了中文、数字 12345 和 ASCII 混排。")
# --tool-call 时的固定调用：一个字符串参数 + 一个数字参数。数字那个是刻意的——解析侧会
# 把它转成 Python 的 int，渲回 prompt 时如果不用原文，`3` 这轮还能对上、`true`/`1.50`
# 那种就对不上了（见 toolcalls.render_assistant_turn）。
TOOL_CALL = ("\n\n<tool_call>\n<function=get_weather>\n<parameter=city>\n北京\n"
             "</parameter>\n<parameter=days>\n3\n</parameter>\n</function>\n</tool_call>")
CTX_LIMIT = 4096


def emit(out, tag, head, payload=b""):
    """一帧一次性写完整（真后端用 g_output_mutex 做同样的事）。"""
    with emit.lock:
        out.write(("%s%s\n" % (tag, head)).encode("utf-8"))
        if payload:
            out.write(payload)
        out.flush()


emit.lock = threading.Lock()


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--sessions", type=int, default=2)
    ap.add_argument("--serve-fd", type=int, default=None)
    ap.add_argument("--default-n", type=int, default=512)
    ap.add_argument("--delay", type=float, default=0.30)
    # 让回复以 `<think>  </think>  ` 开头，用来端到端验网关的 ThinkStripper：它必须把
    # 这段摘掉，而且**标签被 7 字节切分切开时也要摘对**。
    #
    # 注意它现在只是**兜底**形态、不再是"关思考时的真实形态"了：2026-09-18 起网关在
    # 关思考时按模板把 `<think>\n\n</think>\n\n` 预填进 prompt（rkllm_gateway.THINK_OFF），
    # 真模型那条路根本不吐这个段。留着它是为了钉住另一件事——**客户端只要送来
    # enable_thinking=false，不管后端起手吐什么，那一段都不许漏到屏幕上**。
    ap.add_argument("--think-prefix", action="store_true")
    # 让桩也能造出"上下文装不下"那条路径（真后端在 prefill **之前**判，发 CLEAR+REJECT，
    # **不**标死会话）。0 = 不限。有它才能把网关那半边（软错误 / 不标死 / 作废粘性 / 不
    # 把这轮 prompt 记进 KV 账）在本地测到，否则那四条只有板卡上把上下文撑满才能验。
    ap.add_argument("--ctx-limit", type=int, default=0)
    # 让桩先回一个工具调用、拿到 tool 结果之后再正常回答，好把"请求 -> 注入工具说明 ->
    # 生成 -> 摘出调用 -> OpenAI 形态响应 -> 回灌 tool 结果 -> 再问一轮"整条回路在没有
    # 板卡的情况下走通。判据是 prompt 里有没有 <tool_response>（由对话本身决定，不需要
    # 桩自己记状态），所以也顺带验了网关把 tool 结果正确渲进了下一轮 prompt。
    ap.add_argument("--tool-call", action="store_true")
    # 让桩**每一轮都调用**，而不是"调一次、拿到 tool 结果就好好回答"。真模型就是这样：
    # 2026-09-17 板上实测，它回灌了工具结果之后又吐了一次工具调用。
    #
    # 为什么非要有这个模式：serve_http_test.py tools 里那条 KV 判据下一轮的助手消息，
    # 原来只回显了**正文**、把 tool_calls 丢了。桩只在第一轮调用，那一轮的正文恰好非空，
    # 于是"丢调用"这个错法在桩上完全看不出来；真模型一调，助手轮少一个字段，前缀全断、
    # `cached_tokens` 归零，板上直接红。桩默认行为不变（调一次），要复现这个场景就用它。
    ap.add_argument("--tool-call-repeats", action="store_true",
                    help="每一轮都回工具调用（模仿真模型），而不是拿到 tool 结果就收手")
    # STAT 帧的合成数字用。真后端是 4 个 stage（每卡一个），桩默认也报 4 张卡，
    # 但**卡的编号与请求的会话号无关**——桩里没有流水线，随便挑一张记账就行。
    ap.add_argument("--stat-cards", type=int, default=4)
    ap.add_argument("--ctx-len", type=int, default=8192)
    # 模拟"旧后端"：READY 只发两个字段（不报真实上下文），于是网关不知道后端是否支持
    # COUNT/SAVE/LOAD 新帧，会保守地关闭窗口滚动与换页。用它可以复现"没重编后端"的场景，
    # 验证网关不会往旧后端发 COUNT 把每条请求都拖到超时。
    ap.add_argument("--legacy-ready", action="store_true")
    args, _unknown = ap.parse_known_args()

    if args.serve_fd is not None:
        out = os.fdopen(args.serve_fd, "wb")
    else:
        # 桩模式下网关把帧从我们的 stdout 读（Windows 上 pass_fds 不可用）
        out = sys.stdout.buffer

    if args.legacy_ready:
        emit(out, "READY ", "%d %d" % (args.sessions, args.default_n))
    else:
        emit(out, "READY ", "%d %d %d"
             % (args.sessions, args.default_n,
                args.ctx_limit if args.ctx_limit > 0 else args.ctx_len))
    sys.stderr.write("[stub] ready: sessions=%d default_n=%d\n" % (args.sessions, args.default_n))
    sys.stderr.flush()

    ctx = [0] * args.sessions
    ctx_lock = threading.Lock()
    workers = []

    # 合成资源账本。与启动闸同一条线性式：32K 下每路 406.7 MB/卡 ≈ 12.4 KB/token/卡，
    # 每卡权重+内部占完之后剩 1078 MB 可分配给 KV。`node_min_free` 比整卡空闲更紧
    # （真机 32K 两路时整卡还剩 406 MB、最紧的 node 只剩 12.6 MB），这里按 1/8 造。
    kv_per_session = max(1, args.ctx_len) * 12400
    mem_total = 5071 * 1024 * 1024
    mem_free = max(0, 1078 * 1024 * 1024 - args.sessions * kv_per_session)
    stat_lock = threading.Lock()
    stat_cards = [{"name": "stage%d" % i,
                   "busy_us": 0,
                   "run_calls": 0,
                   "mem_total": mem_total,
                   "mem_free": mem_free,
                   "node_num": 8,
                   "node_min_free": mem_free // 8,
                   "ctx_len": args.ctx_len}
                  for i in range(max(1, args.stat_cards))]
    stat_mem_at_us = int(time.monotonic() * 1e6)

    def serve(rid, session, max_new, reset, prompt):
        s = session if 0 <= session < args.sessions else 0
        prefill = max(1, len(prompt.encode("utf-8")) // 5)
        with ctx_lock:
            if reset and ctx[s] > 0:
                emit(out, "CLEAR ", "%d %d %d %d" % (rid, s, ctx[s], CTX_LIMIT))
                ctx[s] = 0
            # 上下文装不下就**先拒掉这一轮**，顺序与真后端一致（见 main.cc 的
            # context_almost_full 分支）：先 CLEAR（让网关作废这段对话的粘性记录），
            # 再 REJECT（会话还活着，只是这一轮不跑），最后把 KV 当已清。
            # 不这么做的话，网关只发增量，KV 一清模型就拿增量去拼一段没有开头的对话，
            # 然后以一个自信的错答案正常返回。
            was = ctx[s]
            reject = (args.ctx_limit > 0 and was > 0 and
                      was + prefill + 512 >= args.ctx_limit)
            if reject:
                emit(out, "CLEAR ", "%d %d %d %d" % (rid, s, was, args.ctx_limit))
                ctx[s] = 0
        if reject:
            msg = ("context limit reached: %d + 512 >= %d tokens; start a new "
                   "conversation or trim the history" % (was, args.ctx_limit))
            emit(out, "REJECT ", "%d %d %d" % (rid, s, len(msg)), msg.encode("utf-8"))
            return
        # 忙时只算这一小段 sleep（真后端算的是 `rknn3_session_run` 的墙钟）。桩里这就
        # 等价于"设备在干活"，好让网关的 busy_pct 有东西可算、页面上的数字会动。
        card = stat_cards[s % len(stat_cards)]
        t_busy0 = time.monotonic()
        time.sleep(args.delay)
        busy_us = int((time.monotonic() - t_busy0) * 1e6)
        with stat_lock:
            card["busy_us"] += busy_us
            card["run_calls"] += 1
        # 切得要"不整齐"：把多字节字符也切开，逼网关用增量解码器
        reply = ("<think>  </think>  " + REPLY) if args.think_prefix else REPLY
        if args.tool_call and (args.tool_call_repeats or "<tool_response>" not in prompt):
            reply += TOOL_CALL
        raw = reply[:max_new].encode("utf-8")
        step = 7
        for i in range(0, len(raw), step):
            emit(out, "DELTA ", "%d %d" % (rid, min(step, len(raw) - i)), raw[i:i + step])
        time.sleep(0.05)
        with ctx_lock:
            ctx[s] += prefill + len(raw)
            after = ctx[s]
        # 判据按"发出去的字节是不是比整段回复短"来定。原版是
        # `finish = "length" if len(REPLY) <= max_new else "stop"`，两个错叠在一起：
        #   · **反了**——回复装得下（没被截断）反而被报成 `length`；
        #   · **单位混用**——`len(REPLY)` 是字符数，`max_new` 是 token 预算。
        # 后果不是"桩看起来不对"：本地全套回归里凡是拿 finish_reason 判"这一轮有没有
        # 被截断"的断言，在桩上都拿到反的答案（真板卡上是 `stop`，桩上是 `length`）。
        finish = "length" if len(raw) < len(reply.encode("utf-8")) else "stop"
        emit(out, "DONE ", "%d %d %s %d %d %.1f %.1f %d"
             % (rid, s, finish, prefill, min(len(REPLY), max_new),
                prefill * 50.0, min(len(REPLY), max_new) * 90.0, after))

    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            continue
        if line == b"QUIT":
            break
        if line == b"STAT":
            # 资源快照。真后端在**输入线程上就地**答（只读原子量、不碰设备、不进队列），
            # 所以解码再久也照样秒回；桩没有设备也没有队列，照抄"随时可答"这条契约。
            # ⚠️ 这帧**必须在读循环里被消费掉**：网关那头也认它、也把载荷读走，两边
            # 少一边就会整条帧流错位（就是 ERR 帧咬过一次的那个坑）。
            with stat_lock:
                cards = [dict(c, mem_at_us=stat_mem_at_us) for c in stat_cards]
            body = json.dumps({"now_us": int(time.monotonic() * 1e6), "cards": cards},
                              separators=(",", ":")).encode("utf-8")
            emit(out, "STAT ", "%d" % len(body), body)
            continue
        if line.startswith(b"COUNT "):
            # 改动 A 的精确计数帧（真后端在输入线程上就地 tokenize 一次）。桩没有
            # tokenizer，用与 serve() 里 prefill 估算**同一把尺子**（字节/5）代替，
            # 好让网关的窗口预算在本地与真后端算的是同一个量级。
            try:
                parts = line.split()
                if len(parts) != 3:
                    raise ValueError("bad count header: %r" % (line,))
                c_rid = int(parts[1])
                c_n = int(parts[2])
                c_payload = sys.stdin.buffer.read(c_n) if c_n > 0 else b""
                if len(c_payload) != c_n:
                    raise ValueError("short read: want %d got %d" % (c_n, len(c_payload)))
            except Exception as exc:
                sys.stderr.write("[stub] FRAME STREAM CORRUPT: %r\n" % (exc,))
                sys.stderr.flush()
                emit(out, "ERR ", "0 - %d" % len(str(exc)), str(exc).encode("utf-8"))
                return 2
            emit(out, "COUNTED ", "%d %d" % (c_rid, max(1, len(c_payload) // 5)))
            continue
        if line.startswith(b"SAVE ") or line.startswith(b"LOAD "):
            # 改动 C 的会话换页帧（真后端由会话线程逐卡存/取）。桩不连板卡，用一个小
            # 标记文件代替每段 KV：SAVE 写 `<path>_seg0.kv`，LOAD 检查它在不在。
            # 这样网关的"换页后读回、只发增量"这条记账在本地也能端到端走通。
            try:
                parts = line.split()
                is_load = (parts[0] == b"LOAD")
                if is_load:
                    if len(parts) != 5:
                        raise ValueError("bad load header: %r" % (line,))
                    o_rid = int(parts[1])
                    o_n = int(parts[4])
                else:
                    if len(parts) != 4:
                        raise ValueError("bad save header: %r" % (line,))
                    o_rid = int(parts[1])
                    o_n = int(parts[3])
                payload = sys.stdin.buffer.read(o_n) if o_n > 0 else b""
                if len(payload) != o_n:
                    raise ValueError("short read: want %d got %d" % (o_n, len(payload)))
            except Exception as exc:
                sys.stderr.write("[stub] FRAME STREAM CORRUPT: %r\n" % (exc,))
                sys.stderr.flush()
                emit(out, "ERR ", "0 - %d" % len(str(exc)), str(exc).encode("utf-8"))
                return 2
            path = payload.decode("utf-8", "replace")
            seg = (path[:-3] + "_seg0.kv") if path.endswith(".kv") else (path + "_seg0.kv")
            ok = 1
            try:
                if is_load:
                    ok = 1 if os.path.exists(seg) else 0
                else:
                    with open(seg, "wb") as fh:
                        fh.write(b"stub-kv")
            except OSError:
                ok = 0
            emit(out, "LOADED " if is_load else "SAVED ", "%d %d" % (o_rid, ok))
            continue
        try:
            parts = line.split()
            if parts[0] != b"REQ" or len(parts) != 6:
                raise ValueError("bad header: %r" % (line,))
            rid = int(parts[1])
            session = int(parts[2])
            max_new = int(parts[3])
            reset = int(parts[4])
            n = int(parts[5])
            payload = sys.stdin.buffer.read(n) if n > 0 else b""
            if len(payload) != n:
                raise ValueError("short read: want %d got %d" % (n, len(payload)))
        except Exception as exc:
            # 头行解析不了 = 帧流错位了（多半是网关那边写 stdin 没整体加锁，
            # 两条线程的头行和载荷互相插了）。显式炸掉，不要静默丢帧。
            sys.stderr.write("[stub] FRAME STREAM CORRUPT: %r\n" % (exc,))
            sys.stderr.flush()
            emit(out, "ERR ", "0 - %d" % len(str(exc)), str(exc).encode("utf-8"))
            return 2
        # max_new_tokens 超范围：真后端（main.cc）2026-09-16 起会在 prefill 前回 ERR——
        # 那个字段最终落到一个 `int` 上，不校验的话大数会静默截断成别的值（10**12 变成
        # 0 = "用默认值"）。桩跟着实现同一条契约，好让"网关有没有把它夹进范围"
        # （`_pick_max_tokens`）在本地也测得出来。**载荷必须已经读掉**再判——否则流错位。
        if max_new > 0x7fffffff:
            msg = b"max_new_tokens out of range: %d" % max_new
            emit(out, "ERR ", "%d %d %d" % (rid, session, len(msg)), msg)
            continue
        th = threading.Thread(target=serve,
                             args=(rid, session, max_new, reset,
                                   payload.decode("utf-8", "replace")))
        th.start()
        workers.append(th)

    for th in workers:
        th.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
