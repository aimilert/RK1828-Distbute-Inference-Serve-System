#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""粘性 KV 复用的合取核对：既看「有没有复用」，也看「复用之后模型还记不记得」。

背景（为什么要这么绕）：
  网关记的 known[s] = 上一轮整段 prompt + 上一轮回复，下一轮只发差异部分。这里有个
  推理想不清楚的隐患——分词是左到右贪婪 BPE，**不是按前缀可分的**：上一轮回复的结尾
  和这一轮后缀在拼接处可能合并成别的 token。于是「KV 里的 token + 新后缀的 token」
  和「整段重新分词」在边界那一个 token 上可能不同。后果不是崩溃，是模型看到的上下文
  悄悄差一点，答出来的东西看着通顺。这种错只能靠实测。

判据为什么是语义的：
  实测这套栈**本来就不是逐位可复现**的（determinism_probe.py：同 session 同 prompt
  连打 3 次，回答有 2 种）。所以「两次回答逐字节相同」会把固有抖动误判成复用 bug。
  改用**信息存活**判据：第一轮给模型一个暗号，第二轮的提问故意不含暗号、也不含第一轮
  的回复——粘性路径下暗号只可能来自会话 KV。答得出 => KV 真的装住了那段历史且拼接
  正确；答不出 => 复用把上下文弄丢了，必须查。

判据也不要只看 usage：`prompt_tokens` 的口径是**整段 prompt**（OpenAI 口径，2026-09-16
起网关已改正），复用在它里面体现为 `prompt_tokens_details.cached_tokens > 0`。但它仍然
不够——复用可能发生在**别的会话**上（换了会话也可能碰巧前缀相同）。所以同时看
X-KV-Reuse 响应头（会话号 / 是否 RESET / 实发字节数）。

用法：python3 sticky_check.py http://127.0.0.1:18280
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18280"
TIMEOUT = 1800
# 暗号要中性：第一版用了「紫色犀牛731」，模型把它读成历史敏感事件直接拒答，
# 实验就废了（踩过）。下面这个是纯无害的名词短语。
NEEDLE = "青柠味苏打水"
FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def close_conv(conv):
    """告诉网关"这段对话结束了"，把会话立刻还回去（不等 IDLE_TTL）。"""
    body = json.dumps({"conversation_id": conv}).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/conversations/close", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_kv(kv):
    """把 `X-KV-Reuse` 头拆成 dict（`reset=0; base=403; sent=64; full=467; ...`）。"""
    if not kv:
        return {}
    out = {}
    for part in kv.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def chat(messages, conv, max_tokens=96):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": False, "conversation_id": conv}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8"))
        kv = r.headers.get("X-KV-Reuse")
    c = d["choices"][0]
    # usage.prompt_tokens 是**整段 prompt**的长度（OpenAI 口径）；本轮真正重算的部分
    # 要减掉被复用的那部分（prompt_tokens_details.cached_tokens）。2026-09-16 之前网关
    # 报的是增量，那时这个脚本直接拿 prompt_tokens 当"prefill 量"用——口径改了，
    # 所以这里显式把两个数都取出来，判据看 recomputed。
    u = d["usage"]
    return (c["message"]["content"], u["prompt_tokens"],
            u.get("prompt_tokens_details", {}).get("cached_tokens", 0),
            c["finish_reason"], kv, time.time() - t0)


print("=== 粘性复用核对：%s ===" % BASE)

print("== 1. 第一轮：把暗号告诉模型 ==")
a1, p1, c1, f1, kv1, t1 = chat([{"role": "user", "content":
                          "记住暗号：%s。收到请只回复两个字：收到。" % NEEDLE}],
                        "keep-1", 96)
print("     prompt=%d tok（复用 %d）, finish=%s, kv=[%s]" % (p1, c1, f1, kv1))
print("     答=%r" % a1[:100].replace("\n", " "))
check("第一轮回答非空", len(a1) > 0)
check("第一轮没被截断（否则第二轮结果不可解释）", f1 == "stop", "finish=%s" % f1)

hist = [{"role": "user", "content": "记住暗号：%s。收到请只回复两个字：收到。" % NEEDLE},
        {"role": "assistant", "content": a1}]
Q = {"role": "user", "content": "暗号是什么？只回答暗号本身，不要解释。"}

print("== 2. 第二轮走粘性复用（同一 conversation_id）==")
a_s, p_s, c_s, f_s, kv_s, t_s = chat(hist + [Q], "keep-1", 96)
print("     prompt=%d tok（复用 %d，本轮只算 %d）, finish=%s, kv=[%s]"
      % (p_s, c_s, p_s - c_s, f_s, kv_s))
print("     答=%r" % a_s[:100].replace("\n", " "))

print("== 3. 第二轮走全量 prefill（换 conversation_id => 无粘性记录）==")
a_f, p_f, c_f, f_f, kv_f, t_f = chat(hist + [Q], "keep-2", 96)
print("     prompt=%d tok（复用 %d，本轮只算 %d）, finish=%s, kv=[%s]"
      % (p_f, c_f, p_f - c_f, f_f, kv_f))
print("     答=%r" % a_f[:100].replace("\n", " "))

print("== 4. 判定 ==")
check("粘性那轮更快（少算了 prefill 的历史部分）", t_s < t_f,
      "%.1fs vs %.1fs" % (t_s, t_f))
# 这一条以前是 `sent < len(json.dumps(hist)) * 4` —— **恒真式**（阈值约 1000，而同一
# 个响应头里就写着 full=366，sent ≤ full 永远成立）。改成看响应头自己给的两个数：
# 有可复用的 base，且本轮发出去的确实比全量少。
_kv_s = parse_kv(kv_s)
check("粘性那轮确实只发了差异部分（响应头 base>0 且 sent < full）",
      bool(_kv_s) and _kv_s.get("reset") == "0" and int(_kv_s.get("base", 0)) > 0
      and int(_kv_s.get("sent", 1)) < int(_kv_s.get("full", 0)),
      "kv=[%s]" % kv_s)
# 这一条也换过口径（同 serve_http_test.py）：prompt_tokens 现在是整段 prompt，
# "更小"要拿**本轮真正重算的部分**（整段 − 复用）去比。
check("粘性那轮真正重算的部分明显更小",
      (p_s - c_s) * 2 < p_f and c_s > 0,
      "续聊重算=%d tok（复用 %d）, 对照全量=%d tok" % (p_s - c_s, c_s, p_f))
check("粘性路径答出了暗号（KV 装住了历史且拼接正确）", NEEDLE in a_s)
check("全量路径答出了暗号（对照组本身没问题）", NEEDLE in a_f)

# 用完把会话交还。**这不是客气**：板卡上只有 4 个会话，而 run_all_board_tests.sh 是
# 一串脚本连着跑（这个之后还有 nothink_check、http_scaling），谁都不还的话后面那几个
# 就要一路排队——量到的是排队不是并发，而且 `IDLE_TTL` 设成 0 时（本地桩常这么起）
# 根本没有回收，后面直接 503。这类"脚本之间的会话泄漏"修过一条：http_scaling 的
# warmup 就是这么把 N=4 那批挡住的。
for _c in ("keep-1", "keep-2"):
    close_conv(_c)

print("\n%s" % ("全部通过" if not FAILS else "失败项：%s" % ", ".join(FAILS)))
sys.exit(0 if not FAILS else 1)
