#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4 路并发的**看得见**演示：4 条 SSE 流同时吐字。

http_scaling.py 量的是数，这个脚本要的是**眼睛能看见**：屏幕上 4 行一起往前爬，
这是"真的在并发"最直接的证据。反过来，如果看到 4 行是**轮流**动、一次只有一行在爬，
那就是并发被吃掉了——网关侧最常见的元凶是会话租约算错（方案文档 §9.7 坑 4），
日志里的租约落点会露出马脚。

三个刻意的设计，都有原因：

1. **每一路都显式带自己的身份**（`X-Conversation-Id: demo4-<路号>`），而不是靠"首条 prompt
   不同"去让网关认出这是 4 段对话。内容认对话是**会撞的**：一旦两路问同一句话，它们就是
   同一段对话、落到同一个会话上排队，而答案全对、肉眼只看得出"这路慢了点"。
   多人接入时靠内容认身份就是这个下场——这里改用显式身份，既稳当，也是给观众的正面示范。
   （v1.9 之前靠内容不同，前提是"4 路一定问不一样的话"；现在不依赖这个前提了。）
2. usage 走 `stream_options.include_usage`（OpenAI 的字段）：拿到的是后端自己数的
   decode token 数，和 http_scaling.py 同一口径。不这么写就只能数 SSE 块，而**块 ≠ token**，
   演示出来的数会和测量脚本对不上。
3. 先量一遍 N=1 做基线，再量 N 路，最后把两个数并排打出来——只报一个绝对数字说明不了
   "并发有收益"，有对照才有意义。

用法：python3 demo_4session.py [base_url] [N] [每路token数] [--plain] [--nothink]
  默认 http://127.0.0.1:18280 / N=4 / 128 token / 开思考
  --plain    不用光标重绘（输出要重定向到文件或串口时用），改成逐块打带标签的行
  --nothink  关思考（软开关：网关给每条 user 贴 /no_think 并摘掉 <think> 段）。
             **上台演示建议加这个**：默认开思考时，流出来的是推理过程
             （实测会吐 "Evaluate Safety and Policy" 之类），投影给观众看很怪。
"""
import json
import sys
import threading
import time
import urllib.request

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
PLAIN = "--plain" in sys.argv
NOTHINK = "--nothink" in sys.argv
BASE = ARGS[0] if len(ARGS) > 0 else "http://127.0.0.1:18280"
N = int(ARGS[1]) if len(ARGS) > 1 else 4
NP = int(ARGS[2]) if len(ARGS) > 2 else 128
TIMEOUT = 1800

# 提示词：让每路都吐**看得见、可数**的内容，且每路的开头不同（见文件头第 1 条）。
PROMPT = "第 %d 路演示：请从 1 数到 300，只写数字，用空格分隔。"


def one_stream(i, out, state, lock):
    """跑完第 i 路（i 是 0 基下标）；实时把增量画到 out 上（PLAIN 时逐块打行）。

    注意 i 是 0 基的：它既用来取 state[i]，也用来生成"第 i+1 路"的提示词。
    把 1 基的路号直接当 state 下标用过一次，N=1 时当场 IndexError（state[1] 越界）。
    """
    tag = i + 1
    body = {"model": "qwen3.5-27b",
            "messages": [{"role": "user", "content": PROMPT % tag}],
            "max_tokens": NP, "stream": True,
            "stream_options": {"include_usage": True}}
    if NOTHINK:
        body["chat_template_kw"] = {"enable_thinking": False}
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        # 身份显式给（见文件头第 1 条）。注意**基线那一路（N=1）用的是同一个 id**，
        # 这样"再量 4 路"时它命中同一段对话——仍然是 4 段对话 4 个会话，占比是对的。
        headers={"Content-Type": "application/json",
                 "X-Conversation-Id": "demo4-%d" % tag})
    t0 = time.time()
    ntok = 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
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
            # 末尾那块带 usage（include_usage 的作用），以后端自数的 decode 数为准
            u = d.get("usage") or {}
            if isinstance(u.get("completion_tokens"), int):
                ntok = u["completion_tokens"]
            choices = d.get("choices") or [{}]
            piece = (choices[0].get("delta") or {}).get("content")
            if not piece:
                continue
            with lock:
                state[i]["text"] += piece
                state[i]["chunks"] += 1
                if not PLAIN:
                    draw(state, out)
                else:
                    out.write("[s%d] %s\n" % (tag, piece))
                    out.flush()
    dt = time.time() - t0
    # usage 没到（理论上不会）就退化成数出来的块数，并在结果里标出来
    return dt, ntok, len(state[i]["text"])


def draw(state, out):
    """把 N 行状态就地重画一遍（ANSI：上移 N 行再逐行覆写）。"""
    if state and state[0].get("drawn"):
        out.write("\033[%dA" % len(state))
    lines = []
    for st in state:
        text = " ".join(st["text"].split())
        tail = text[-(W_TEXT):]
        el = st["dt"] if st["done"] else (time.time() - st["t0"])
        rate = (st["tok"] / el) if (el > 0 and st["tok"]) else 0.0
        lines.append("%s │%-*s│ %4d tok %5.1fs %5.1f tok/s\n"
                     % (st["tag"], W_TEXT, tail, st["tok"], el, rate))
        st["drawn"] = True
    out.write("\x1b[K\n".join(x.rstrip("\n") for x in lines) + "\x1b[K\n")
    out.flush()


W_TEXT = 42


def run_batch(n):
    """同时起 n 条流，返回 (墙钟秒, [[秒, token, 字符数], ...])。"""
    state = [{"tag": "[s%d]" % (i + 1), "text": "", "chunks": 0, "tok": 0,
              "t0": time.time(), "dt": 0.0, "done": False} for i in range(n)]
    res = [None] * n
    lock = threading.Lock()
    out = sys.stdout

    def worker(i):
        try:
            dt, ntok, nchar = one_stream(i, out, state, lock)
            with lock:
                state[i]["tok"], state[i]["dt"], state[i]["done"] = ntok, dt, True
                if not PLAIN:
                    draw(state, out)
            res[i] = (dt, ntok, nchar)
        except Exception as exc:                              # noqa: BLE001
            res[i] = (-1.0, 0, "FAILED: %r" % (exc,))

    t0 = time.time()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return time.time() - t0, res


print("=== 4 路并发演示：%s（每路 max_tokens=%d）===" % (BASE, NP))
try:
    h = json.loads(urllib.request.urlopen(BASE + "/health", timeout=10).read()
                   .decode("utf-8"))
except Exception as exc:                                      # noqa: BLE001
    print("连不上网关 %s：%r" % (BASE, exc))
    sys.exit(2)
print("网关: %s  模型=%s  后端会话数=%d" % (h.get("status"), h.get("model"),
                                         h.get("sessions")))
if h.get("sessions", 0) < N:
    print("⚠️ 后端只有 %d 个会话，要跑 %d 路并发请用 NSESSION=%d 重启网关"
          % (h.get("sessions", 0), N, N))

print("\n--- 第 1 步：基线（N=1，单请求）---")
if not PLAIN:
    print("(单路不画进度条，直接等它跑完)")
wall1, res1 = run_batch(1)
if res1[0] is None or res1[0][0] < 0:
    print("基线失败：%r" % (res1[0],))
    sys.exit(1)
tok1 = res1[0][1] or res1[0][2]
base_rate = tok1 / wall1
print("单请求：%d tok / %.1fs = **%.2f tok/s**" % (tok1, wall1, base_rate))

print("\n--- 第 2 步：%d 路同时发（看这 %d 行是不是一起往前爬）---" % (N, N))
if not PLAIN:
    print("(下面是实时重绘的 %d 行，每行一路；跑完会定格)" % N)
wall_n, resn = run_batch(N)
for i, r in enumerate(resn):
    if r is None or r[0] < 0:
        print("第 %d 路失败：%r" % (i + 1, r))
        sys.exit(1)

total = sum(r[1] or r[2] for r in resn)
agg = total / wall_n
print("\n--- 结果 ---")
for i, r in enumerate(resn):
    print("  [s%d] %d tok / %.1fs = %.2f tok/s" % (i + 1, r[1] or r[2], r[0],
                                                   (r[1] or r[2]) / r[0]))
print("  ── %d 路合计 %d tok / 墙钟 %.1fs" % (N, total, wall_n))
print("     聚合 %.2f tok/s   基线 %.2f tok/s   **伸缩 %.2fx**"
      % (agg, base_rate, agg / base_rate))
print("     单请求延迟 %.1fs → %.1fs（并发不是零代价）"
      % (wall1, sum(r[0] for r in resn) / len(resn)))


def close_all():
    """问完就把这几段对话还回去。

    不做这一步的话，网关会一直替它们占着会话到 `IDLE_TTL` 超时（默认 300 秒）——
    演示时"再点一次②"就会因为拿不到会话而失败，而且失败的样子是**基线那路报 503**，
    看起来像并发坏了，其实是上一次自己没走。网关不知道对话什么时候结束，得客户端说。
    """
    for tag in range(1, N + 1):
        try:
            req = urllib.request.Request(
                BASE + "/v1/conversations/close",
                data=json.dumps({"conversation_id": "demo4-%d" % tag}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:                                     # noqa: BLE001
            pass
    print("(已交还 %d 段对话的会话，可以直接重跑)" % N)


close_all()
