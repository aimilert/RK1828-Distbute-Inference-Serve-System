#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把板卡当**边缘服务器**用：N 个用户各自从终端接入，各占一个会话。

和 demo_4session.py 的区别（两个脚本要演示的是两件不同的事）：
  · demo_4session.py  —— 4 路**同时**发问，证明并发的吞吐是 3.3x 而不是排队。
  · 本脚本            —— N 个用户**各有各的身份**，随时来随时问；证明会话是按用户分配的，
                        第 N+1 个用户会**排队等待**而不是把别人的会话抢走。

三个刻意的设计：

1. **每个用户发的是同一句话**。这是本脚本最重要的设计。网关在没人告诉它"你是谁"时，
   是按「system + 首条提问」的摘要来认对话的——同一句话 = 同一段对话 = 被钉到同一个
   会话上串行。所以这里所有用户都问同一句话，靠 `X-Conversation-Id: u<i>` 把身份显式
   给出去。**加 `--no-id` 就能看到不带身份时会发生什么**：四个用户退化成排队，聚合吞吐
   掉到接近单路。多人接入时不传这个标识，就是这个下场，而且答案全对、看不出错。

2. **错开提问**（`--stagger`，默认 1.5s）。同时发问演示的是并发；错开才演示得出**排队**：
   后面的用户到达时前面的还在生成，会话全忙，于是他进队列；等谁先跑完，他立刻补上。
   这一刻在屏幕上就是「用户 5 的等待秒数停住 → 突然开始出字」。

3. **状态从 `GET /v1/pool` 读**。用户自己在 `urlopen` 返回之前是拿不到任何信息的
   （POST 阻塞着），所以"谁在排队"只能从旁路看。这个端点存在的意义就是这个：
   **多用户场景下"某个人在排队"从吞吐上是看不出来的**——他没变慢，他是拿不到。

用法：
  python3 demo_multiuser.py [base_url] [用户数] [每轮token数]
      [--stagger 秒] [--rounds 轮数] [--plain] [--nothink] [--no-id] [--close]

  默认 http://127.0.0.1:18280 / 6 个用户 / 96 token / 错开 1.5s / 1 轮
  --plain    不用光标重绘（重定向到文件或录屏时用），改成打带时间戳的行
  --nothink  关思考（软开关，见方案文档 §9.7）
  --no-id    不给 conversation_id —— 用来演示"不带身份会退化成串行"
  --close    用户问完就调 `POST /v1/conversations/close` 把会话还回去（见文件尾的说明）
"""
import json
import sys
import threading
import time
import urllib.request

# Windows 控制台是 GBK 时，下面的 ⚠️ 会让 print 抛 UnicodeEncodeError（**不是**显示乱码，
# 是直接崩）。板卡上是 UTF-8 用不着这段，但演示脚本常从 Windows 终端连上去跑，
# 所以这里兜一下：不可编码的字符退化成 "?"，而不是整个脚本挂掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

PLAIN = "--plain" in sys.argv
NOTHINK = "--nothink" in sys.argv
NO_ID = "--no-id" in sys.argv
CLOSE = "--close" in sys.argv

# 带值的开关必须把**它的值**一起跳过，否则 `--stagger 0.6` 里的 "0.6" 会被当成
# 位置参数吃掉（ARGS[2] 是 token 数）→ int("0.6") 直接抛异常。
VALUED = ("--stagger", "--rounds")
argv = list(sys.argv[1:])
ARGS = []
i = 0
while i < len(argv):
    a = argv[i]
    if a in VALUED:
        i += 2
        continue
    if a.startswith("--"):
        i += 1
        continue
    ARGS.append(a)
    i += 1

BASE = ARGS[0] if len(ARGS) > 0 else "http://127.0.0.1:18280"
NUSERS = int(ARGS[1]) if len(ARGS) > 1 else 6
NP = int(ARGS[2]) if len(ARGS) > 2 else 96
TIMEOUT = 1800


def opt(name, default):
    flag = "--" + name
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return float(sys.argv[i + 1])
    return default


STAGGER = opt("stagger", 1.5)
ROUNDS = int(opt("rounds", 1))

# **所有人问同一句话**，见文件头第 1 条。这既是本脚本要演示的东西，也是它最容易
# 被误读的地方：看到"四个人问一样的问题还是并发"是对的，看到"改成不一样的问题更稳"
# 就理解反了——真正的判据是**你有没有把身份告诉网关**，不是问的内容凑不凑巧。
PROMPT = "请从 1 数到 300，只写数字，用空格分隔。"
W_TEXT = 30


def user_thread(u, state, lock, out, shared):
    """第 u 个用户（0 基）。按 ROUNDS 轮次提问，每轮都带同一个 conversation_id。"""
    st = state[u]
    msgs = [{"role": "user", "content": PROMPT}] if ROUNDS == 1 else None
    for rnd in range(1, ROUNDS + 1):
        if msgs is None:
            msgs = [{"role": "user", "content": PROMPT}]
        body = {"model": "qwen3.5-27b", "messages": msgs, "max_tokens": NP,
                "stream": True, "stream_options": {"include_usage": True}}
        if NOTHINK:
            body["chat_template_kw"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        if not NO_ID:
            headers["X-Conversation-Id"] = "u%d" % (u + 1)
        req = urllib.request.Request(BASE + "/v1/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=headers)
        with lock:
            st["round"] = rnd
            st["phase"] = "connecting"
            st["t_send"] = time.time()
            st["text"] = ""
            st["chunks"] = 0
        t_send = time.time()
        try:
            r = urllib.request.urlopen(req, timeout=TIMEOUT)
        except Exception as exc:                              # noqa: BLE001
            with lock:
                st["phase"] = "error"
                st["err"] = repr(exc)[:80]
                if PLAIN:
                    out.write("%7.2fs %s **出错** %s\n"
                              % (time.time() - wall0, st["tag"], st["err"]))
                    out.flush()
                else:
                    draw(state, out, shared)
            return
        # urlopen 返回 = 响应头到了 = **网关已经拿到会话了**。所以这一段就是排队时长，
        # 不用去猜、也不用问别人。
        waited = time.time() - t_send
        kv = r.headers.get("X-KV-Reuse") or ""
        session = None
        for part in kv.split(";"):
            k, _, v = part.partition("=")
            if k.strip() == "session":
                session = v.strip()
        ntok = 0
        with lock:
            st["phase"] = "streaming"
            st["session"] = session
            st["waited"] += waited
            st["t0"] = time.time()
            if PLAIN:
                out.write("%7.2fs %s 拿到会话 %s（等 %.1fs）\n"
                          % (time.time() - wall0, st["tag"], session, waited))
                out.flush()
            else:
                draw(state, out, shared)
        try:
            for raw in r:
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
                used = d.get("usage") or {}
                if isinstance(used.get("completion_tokens"), int):
                    ntok = used["completion_tokens"]
                choices = d.get("choices") or [{}]
                piece = (choices[0].get("delta") or {}).get("content")
                if not piece:
                    continue
                with lock:
                    st["text"] += piece
                    st["chunks"] += 1
                    st["tok"] = ntok or st["chunks"]
                    st["dt"] = time.time() - st["t0"]
                    if not PLAIN:
                        draw(state, out, shared)
        except Exception as exc:                              # noqa: BLE001
            with lock:
                st["phase"] = "error"
                st["err"] = repr(exc)[:80]
            return
        r.close()
        with lock:
            st["tok"] = ntok or st["chunks"]
            st["dt"] = time.time() - st["t0"]
            st["total_tok"] += st["tok"]
            st["phase"] = "idle"
            # 第二轮把这一轮的回复接上，才是"多轮"。网关那边看到的历史变长，
            # 前缀判据仍然成立 => 这一轮只发差异部分（X-KV-Reuse 的 base > 0）。
            msgs = msgs + [{"role": "assistant", "content": st["text"]},
                           {"role": "user", "content": PROMPT}]
            if PLAIN:
                out.write("%7.2fs %s 会话 %s 答完 %d tok / %.2fs = %.2f tok/s\n"
                          % (time.time() - wall0, st["tag"], session, st["tok"],
                             st["dt"], st["tok"] / st["dt"] if st["dt"] > 0 else 0.0))
                out.flush()
            else:
                draw(state, out, shared)
        if rnd < ROUNDS:
            time.sleep(STAGGER)      # 两轮之间也错开，好让"谁在等"看得清楚
    if CLOSE:
        # 问完就走，主动把会话还回去。网关**不知道**一段对话什么时候结束（客户端不发
        # 结束消息），所以默认只能靠 IDLE_TTL 超时回收——对"来问一句就走"的用法很糟：
        # 一个人连问几个不相干的问题就把会话占住了。close 是那条"我走了"的路。
        try:
            req = urllib.request.Request(
                BASE + "/v1/conversations/close",
                data=json.dumps({"conversation_id": "u%d" % (u + 1)}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            with lock:
                st["closed"] = True
        except Exception:                                     # noqa: BLE001
            pass


def poll_pool(shared, lock, stop):
    """旁路读 `GET /v1/pool`，只为顶部那一行"后端现在什么状态"。

    为什么不拿它去匹配"哪个用户在排队"：用户自己在排队时是**完全静默**的（POST 阻塞着，
    一个字节都没收到），但"他已经等了多久"这个量他手上有——`t_send` 到现在就是。所以
    逐行的等待时长直接算，别去猜池子里哪条 waiting 对应哪个用户（--no-id 时名字对不上，
    越猜越乱）。池子这一路要回答的是另一个问题：**后端整体还剩几个会话**。
    """
    while not stop.is_set():
        try:
            snap = json.loads(urllib.request.urlopen(
                BASE + "/v1/pool", timeout=5).read().decode("utf-8"))
            slots = snap.get("slots", [])
            with lock:
                shared["busy"] = sum(1 for s in slots if s["state"] == "busy")
                shared["free"] = sum(1 for s in slots if s["state"] == "free")
                shared["waiting"] = len(snap.get("waiting", []))
                shared["reaped"] = snap.get("reaped_total", 0)
                shared["ok"] = True
        except Exception:                                     # noqa: BLE001
            with lock:
                shared["ok"] = False
        stop.wait(0.5)


def draw(state, out, shared):
    """就地重画（1 行表头 + N 行用户）。--plain 时不走这里。"""
    if state and state[0].get("drawn"):
        out.write("\033[%dA" % (len(state) + 1))
    lines = []
    now = time.time()
    for st in state:
        phase = st["phase"]
        rate = st["tok"] / st["dt"] if st["dt"] > 0.05 else 0.0
        if phase == "streaming":
            status = "出字中 %.2f tok/s" % rate
        elif phase == "connecting":
            # 请求已发出、响应头还没到 = **要么在排队，要么刚到**。短了就显示"连接中"，
            # 超过 0.5s 那就是在排队——这个秒数是用户实打实等掉的时间。
            el = now - st["t_send"]
            status = ("**排队 %.1fs**" % el) if el > 0.5 else "连接中"
        elif phase == "idle" and st["dt"]:
            status = "答完 %.2f tok/s" % rate
        elif phase == "error":
            status = "**出错**"
        else:
            status = "待机"
        tail = " ".join(st["text"].split())[-W_TEXT:]
        lines.append("%s %-5s │ %-18s │ %s%s\n"
                     % (st["tag"], st["session"] or "--", status,
                        ("第%d轮 " % st["round"]) if ROUNDS > 1 else "", tail))
    if shared.get("ok"):
        head = ("后端 %d 个会话：%d 忙 / %d 空闲 · 队列 %d 人 · 已回收 %d 次 · %s"
                % (SESSIONS, shared["busy"], shared["free"], shared["waiting"],
                   shared["reaped"],
                   "**不带 conversation_id**" if NO_ID else "每人一个 conversation_id"))
    else:
        head = "（/v1/pool 读不到）"
    # 每行都以 \x1b[K 起头清到行尾：上一帧的长尾巴不清掉的话，重画会留下残字。
    out.write(head + "\x1b[K\n"
              + "".join("\x1b[K" + x.rstrip("\n") + "\n" for x in lines))
    out.flush()
    for st in state:
        st["drawn"] = True


SESSIONS = 0

print("=== 多用户边缘服务演示：%s（%d 个用户，每人 %d token，错开 %.1fs）==="
      % (BASE, NUSERS, NP, STAGGER))
try:
    h = json.loads(urllib.request.urlopen(BASE + "/health", timeout=10).read()
                   .decode("utf-8"))
except Exception as exc:                                      # noqa: BLE001
    print("连不上网关 %s：%r" % (BASE, exc))
    sys.exit(2)
SESSIONS = h.get("sessions", 0)
print("网关: %s  模型=%s  后端会话数=%d  (idle_ttl=%ss)"
      % (h.get("status"), h.get("model"), SESSIONS, h.get("idle_ttl_s", "?")))
print("用户 %d 个，会话 %d 个 → 预期：前 %d 个各自拿到会话，其余**排队**"
      % (NUSERS, SESSIONS, SESSIONS))
print("问完的处理：%s"
      % ("主动交还会话（POST /v1/conversations/close）—— 排队的人立刻补上"
         if CLOSE else
         "不主动交还，等 IDLE_TTL（默认 300s）超时回收 —— 排队的人要等 TTL 才动"))
if NO_ID:
    print("⚠️ --no-id：所有人问的是同一句话且不带身份 → 网关按内容摘要认对话，"
          "它们会被当成**同一段对话**钉在同一个会话上，看到的是排队不是并发。")

state = [{"tag": "[u%d]" % (i + 1), "session": None, "phase": "idle", "text": "",
          "chunks": 0, "tok": 0, "dt": 0.0, "t0": 0.0, "t_send": 0.0, "round": 0,
          "waited": 0.0, "total_tok": 0, "pool_wait": None, "pool_held": False,
          "closed": False, "err": ""} for i in range(NUSERS)]
lock = threading.Lock()
out = sys.stdout
stop = threading.Event()
# 池子的状态行单独一份（不复用 state）：它描述的是后端，不属于任何一个用户。
shared = {"busy": 0, "free": 0, "waiting": 0, "reaped": 0, "ok": False}
poller = threading.Thread(target=poll_pool, args=(shared, lock, stop))
poller.daemon = True
poller.start()

wall0 = time.time()
ths = []
for u in range(NUSERS):
    t = threading.Thread(target=user_thread, args=(u, state, lock, out, shared))
    t.daemon = True
    ths.append(t)
    t.start()
    time.sleep(STAGGER)          # 见文件头第 2 条：错开才演示得出排队
for t in ths:
    t.join()
wall = time.time() - wall0
stop.set()
with lock:
    draw(state, out, shared)

print("\n--- 结果 ---")
total = 0
queued = 0
for u, st in enumerate(state):
    total += st["total_tok"]
    if st["waited"] >= 0.5:
        queued += 1
    print("  %s 会话 %-4s 排队 %6.1fs  出字 %5d tok  答完 %.1fs  %s%s"
          % (st["tag"], st["session"] or "--", st["waited"], st["total_tok"],
             st["dt"], "已交还 " if st["closed"] else "",
             ("**%s**" % st["err"]) if st["err"] else ""))
sess_used = sorted({st["session"] for st in state if st["session"]})
print("  ── 墙钟 %.1fs，合计 %d tok，聚合 %.2f tok/s" % (wall, total, total / wall))
print("     落到 %d 个不同会话 %s；%d 个用户排过队"
      % (len(sess_used), sess_used, queued))
if NO_ID:
    print("     ↑ --no-id 时这里通常只有 1 个会话（所有人都被当成同一段对话）")
else:
    print("     ↑ 每人一个会话：%d 个用户各占一个，超过后端会话数的才会排队"
          % min(len(sess_used), NUSERS))
