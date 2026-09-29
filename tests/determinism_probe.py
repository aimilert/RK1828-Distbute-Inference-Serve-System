#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这套栈到底可不可复现？——先量清楚，再决定验收判据怎么写。

起因：`sticky_check.py` 原本用「两条路径的回答逐字节相同」当正确性判据，实测直接
FAIL，而同一次运行里两次**条件完全相同**的全量 prefill 也不相同。如果采样本身就不
可复现，那个判据本身是错的（会把正常的浮点抖动判成复用 bug），必须换成语义判据。

理论上应该可复现：main.cc 里 sampling_param 是 temperature=1.0 / **top_k=1** /
top_p=0.9，top_k=1 就是取 argmax，温度乘不改变 argmax 的顺序，是确定性采样。所以
如果实测不可复现，来源只能是**别处**——比如不同 session/不同卡上的 kernel 在
最后一个 ULP 上不同、prefill 分批形状不同、或 top_k 之后还叠了别的过滤。

这个脚本分三组：
  A. 同一 session、同一 prompt，连打 3 次（验证「同条件是否复现」）
  B. 三个不同 conversation_id => 三个不同 session、同一 prompt（验证跨卡/跨 session）
  C. 同一 session 上第二轮的粘性复用（验证复用那一路和全量那一路的差异量级，
     和 A 组的固有抖动相比是否更大——这是判断「复用有没有引入额外差异」的唯一办法）

用法：python3 determinism_probe.py http://127.0.0.1:18280

**用完全部交还**（2026-09-16 修）：本脚本一共用 6 段对话（A 组 1 + B 组 3 + C 组 2），
而板卡的会话池只有 4 个。以前一个都不 close，前 4 段就占满了池子，第 5 段开始要等
`IDLE_TTL`（默认 300s）——探针看着"在跑"，其实卡在排队上，量出来的时间全是等待。
所以 A 组、B 组各用完立刻交还，C 组跑完再还。
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18280"
TIMEOUT = 1800
NP = 32
# 一个对措辞敏感、但答案确定的短请求
ASK = "只输出三个数字，用空格分隔：7 8 9。不要输出别的任何内容。"


def chat(messages, conv, max_tokens=NP):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": False, "conversation_id": conv}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d["choices"][0]["message"]["content"], d["usage"]["prompt_tokens"]


def close_conv(conv):
    """告诉网关"这段对话结束了"，把会话立刻还回去（不等 IDLE_TTL）。"""
    body = json.dumps({"conversation_id": conv}).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/conversations/close", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def show(tag, outs, prefills):
    uniq = len(set(outs))
    # 标签是 prompt_tok 而不是 prefill：usage.prompt_tokens 的口径是**整段 prompt**
    # （2026-09-16 起网关改成 OpenAI 口径）。这里只拿它做"同条件两次运行是否一致"的
    # 对照，所以口径变化不影响结论，只影响它叫什么名字。
    print("  %s: prompt_tok=%s, 不同回答数=%d/%d" % (tag, prefills, uniq, len(outs)))
    for i, o in enumerate(outs):
        print("      [%d] %r" % (i, o[:70].replace("\n", " ")))
    return uniq


print("=== 可复现性探针：%s ===" % BASE)

print("== A. 同一 conversation_id、同一 prompt，连打 3 次 ==")
oa, pa = [], []
for i in range(3):
    # 同一段对话再发同一条 user：完整 prompt 不以 known(=旧prompt+旧回复) 开头，
    # 所以网关会 RESET 并重发全量 —— 等价于「同一 session 上的同条件重复」。
    o, p = chat([{"role": "user", "content": ASK}], "det-same")
    oa.append(o)
    pa.append(p)
ua = show("同 session 重复", oa, pa)
close_conv("det-same")

print("== B. 三个不同 conversation_id（三个不同 session），同一 prompt ==")
ob, pb = [], []
for i in range(3):
    o, p = chat([{"role": "user", "content": ASK}], "det-sess-%d" % i)
    ob.append(o)
    pb.append(p)
ub = show("跨 session", ob, pb)
for i in range(3):
    close_conv("det-sess-%d" % i)

print("== C. 粘性复用的第二轮 vs 全量 prefill，差异量级 ==")
FILLER = "请记住这个口令：紫色犀牛731。后面我会问你。" * 6
o1, p1 = chat([{"role": "user", "content": FILLER}], "det-sticky", 16)
hist = [{"role": "user", "content": FILLER}, {"role": "assistant", "content": o1}]
q = [{"role": "user", "content": "口令是什么？只回答口令本身。"}]
o_s, p_s = chat(hist + q, "det-sticky", 24)
o_f, p_f = chat(hist + q, "det-full-%d" % 0, 24)
print("     turn1 答=%r (prefill %d)" % (o1[:40].replace("\n", " "), p1))
print("     粘性 prefill=%d, 全量 prefill=%d" % (p_s, p_f))
print("     粘性答=%r" % o_s[:70].replace("\n", " "))
print("     全量答=%r" % o_f[:70].replace("\n", " "))
print("     粘性含口令=%s, 全量含口令=%s"
      % ("紫色犀牛731" in o_s, "紫色犀牛731" in o_f))
close_conv("det-sticky")
close_conv("det-full-0")

print()
print("结论提示：若 A 组就不复现，则任何逐字节判据都不成立，验收只能用语义判据；")
print("若 A 组复现而 B 组不复现，则差异来自「不同 session/卡」，与复用无关。")
