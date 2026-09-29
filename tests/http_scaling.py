#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 层的并发伸缩测量——这是自研门面存在的**唯一理由**。

为什么必须从 HTTP 这一层量，而不是看后端日志：网关/HTTP/SSE 都有可能把并发吃掉
（比如把请求排成一队、或者持着全局锁发帧）。后端日志只证明推理执行器能重叠，不能
证明用户真正拿到的是并发。所以这里用 N 条线程同时打 /v1/chat/completions，量
「墙钟时间内产出的 token 总数」——这就是用户实际感受到的吞吐。

对照口径：官方 rkllm3-server 同样条件实测 N=1/2/4 是 10.63 / 10.98 / 10.98 tok/s
（1.03x，文档 §4.5.2 写明「推理执行排队串行」）；我们的执行器是 3.33x。
用同一份脚本、同一台板子、同样的 N，这两个数才可比。

用法：python3 http_scaling.py http://127.0.0.1:18280 [每路token数] [N列表,逗号分隔]

**为什么每一路都必须显式给身份、用完必须 close**（2026-09-16 修）：
这个脚本以前有个"热身"对话（无身份，靠内容哈希认身份）和 N 路匿名对话，全部**不释放**。
会话池只有 4 个会话、默认 `IDLE_TTL=300s`，所以跑完 warmup + N=1 + N=2 之后池子已经
被 4 段对话占满——**N=4 那一批里有一路要排队**（等满 IDLE_TTL 或 queue_timeout），
量出来的是排队不是并发，而脚本当时**没有任何断言**：rc=0、表照打、数字看着还挺像样。
（上游 `213a131`「只排队不抢占」落地之后就是这样；在那之前的掠夺版会把这个坑盖住。）

现在每一批 N 路都用 `scaling-<N>-<i>` 显式身份，跑完立刻 `close`，并且加了**测量有效性
断言**（见 assert_batch_overlapped）：不是断言"伸缩比应该大于几"（那是被测的量本身），
而是断言"这一批确实重叠了、不是在排队"——排队的话墙钟 ≈ 各路耗时之和，那就不是并发
测量，必须直接失败，不能把无效测量当结果打出去。

（身份必须**纯 ASCII**：它走的是 HTTP 头 `X-Conversation-Id`，而头字段按 latin-1 编
码，写中文会在 urllib 里就抛 UnicodeEncodeError。`伸缩-warmup` 这种名字踩过。）
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18280"
NP = int(sys.argv[2]) if len(sys.argv) > 2 else 96
NS = [int(x) for x in (sys.argv[3].split(",") if len(sys.argv) > 3 else ["1", "2", "4"])]
TIMEOUT = 1800


def chat(prompt, max_tokens, conv):
    body = {"model": "qwen3.5-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "stream": False}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "X-Conversation-Id": conv})
    t = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        session = parse_session(r.headers.get("X-KV-Reuse"))
        d = json.loads(r.read().decode("utf-8"))
    return time.time() - t, d["usage"]["completion_tokens"], session


def parse_session(kv):
    """从 X-KV-Reuse 头里取会话号——这一批到底有没有落在 N 个不同会话上，就靠它。"""
    for part in (kv or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == "session":
                return int(v.strip())
    return None


def close_conv(conv):
    body = json.dumps({"conversation_id": conv}).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/conversations/close", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def waiting_now():
    with urllib.request.urlopen(BASE + "/v1/pool", timeout=30) as r:
        return len(json.loads(r.read().decode("utf-8")).get("waiting", []))


def assert_batch_overlapped(n, wall, lats, sessions):
    """这一批是不是真的重叠了？不是的话直接失败，别把排队当并发。

    判据用的是批次自己的两侧数字，不依赖"应该多少 tok/s"这种被测的量：
      · N 路必须落在 **N 个不同会话**上（同一会话上就是串行）；
      · 墙钟必须明显小于各路耗时之和——完全串行时两者相等（≈1.0），真并发时
        墙钟≈最慢那一路（≈1/N）。取 0.75 当界：排队只要占掉可观比例就会被抓住。
    """
    if n < 2:
        return True, "N=1 无并发可言，跳过重叠判据"
    if len(set(sessions)) != n:
        return False, "N=%d 路只落在 %d 个会话上（%s）=> 有串行" % (n, len(set(sessions)),
                                                                  sessions)
    ratio = wall / sum(lats) if sum(lats) else 0.0
    if ratio >= 0.75:
        return False, ("墙钟 %.1fs / 各路耗时之和 %.1fs = %.2f（≈1 就是排队而非并发）"
                       % (wall, sum(lats), ratio))
    return True, "墙钟/耗时和 = %.2f（<0.75，确实重叠）" % ratio


def run_batch(n, tag):
    res = [None] * n
    keys = ["%s-%d-%d" % (tag, n, i) for i in range(n)]

    def worker(i):
        try:
            # 每路显式身份 + 每路首条 user 都不同 => 网关视为 N 段不同对话 => 落在
            # 不同 session 上。两项都要：光靠内容的话，同一个身份的不同内容会被钉在
            # 同一个会话上串行；光给身份而内容相同也一样。
            res[i] = chat("%s第 %d 路：请从 1 数到 200，只写数字。" % (tag, i), NP, keys[i])
        except Exception as exc:                       # noqa: BLE001
            res[i] = (-1.0, "FAILED: %r" % (exc,), None)

    t0 = time.time()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    # 用完立刻交还：不还的话下一批（N 更大）会没有可用会话，量到的是排队
    for k in keys:
        try:
            close_conv(k)
        except Exception as exc:                       # noqa: BLE001
            print("  [warn] close %s 失败：%r" % (k, exc))
    return wall, res


print("=== HTTP 并发伸缩：%s（每路 max_tokens=%d）===" % (BASE, NP))
print("warmup ...")
try:
    # 热身照留（第一轮要建会话、填 KV，留在测量行里会把 N=1 抬高、伸缩比失真），
    # 但这次**用完就交还**：以前的版本把它留在池子里，后面 N=4 就没会话可用了。
    dt, ntok, sess = chat("热身。", 8, "scaling-warmup")
    close_conv("scaling-warmup")
    print("  warmup: %.1fs, %s tok, 会话=%s（跑完立刻交还）" % (dt, ntok, sess))
except Exception as exc:                               # noqa: BLE001
    print("  warmup 失败：%r" % (exc,))
    sys.exit(2)

base = None
rows = []
bad = False
for n in NS:
    wall, res = run_batch(n, "scaling")
    if any(not isinstance(r[1], int) for r in res):
        print("N=%d 有失败：%r" % (n, res))
        sys.exit(1)
    total = sum(r[1] for r in res)
    agg = total / wall if wall else 0.0
    lats = [r[0] for r in res]
    sessions = [r[2] for r in res]
    good, why = assert_batch_overlapped(n, wall, lats, sessions)
    if not good:
        bad = True
    wait = waiting_now()
    if base is None:
        base = agg
    rows.append((n, wall, total, agg, sum(lats) / len(lats), agg / base if base else 0))
    print("  N=%d: 墙钟 %.1fs, 合计 %d tok, 聚合 %.2f tok/s, 单请求均lat %.1fs, "
          "伸缩 %.2fx, 会话=%s" % (n, wall, total, agg, sum(lats) / len(lats),
                                 agg / base, sessions))
    print("    [%s] 测量有效性：%s（批后排队=%d）"
          % ("PASS" if good else "FAIL", why, wait))

print()
print("| N | 墙钟(s) | 合计tok | 聚合 tok/s | 伸缩比 | 单请求延迟(s) |")
print("|---|---|---|---|---|---|")
for n, wall, total, agg, lat, sp in rows:
    print("| %d | %.1f | %d | %.2f | %.2fx | %.1f |" % (n, wall, total, agg, sp, lat))
print()
print("对照（官方 rkllm3-server，同板同条件）：N=1/2/4 -> 10.63 / 10.98 / 10.98 tok/s，"
      "伸缩 1.03x")
if bad:
    # 无效测量不能只打印一行 FAIL 就走人：这个脚本的输出会被抄进文档和记忆里，
    # 返回 0 就等于给了它一张"可用"的证书。
    print()
    print("**有批次没通过重叠判据（上面标 FAIL）：它量到的是排队不是并发，"
          "那些行不能当伸缩数字用。**")
    sys.exit(1)
