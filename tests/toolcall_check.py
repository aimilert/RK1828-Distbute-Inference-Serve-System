#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真模型会不会照我们的前言吐出一个**可解析**的工具调用？

为什么单独测这一条：其余工具调用的验收都能在桩上跑（`serve_http_test.py tools` 覆盖了
注入 -> 摘出 -> OpenAI 形态 -> 回灌工具结果 -> 跨工具轮的 KV 复用），它们验的是**我们
的实现**。这个脚本验的是**模型**——桩不会告诉你 Qwen3.5-27B 收到我们渲染的工具目录后
到底会不会调用、调用的形状我们认不认得。这两件事必须分开量，因为失败方式完全不同：
桩上全绿而板上一次都不调用，是完全可能的，那时该改的是前言，不是网关。

三种结果，**别混为一谈**：

  · 解出了 tool_calls          => 好，且继续跑下面的回显往返。
  · 正文里残留调用标记         => **FAIL**。模型**说了**，是**我们没听懂**。这是最该
    炸的一种：Agent 拿到一段带 <tool_call> 的正文当普通文本，会一本正经地把它念给
    用户，或者干脆把它当成"模型不想调用"。网关的抽取是无条件的（不看请求里有没有
    tools），所以残留一定意味着解析器与模型对不上，不存在"没传 tools 所以没抽"。
  · 正文是普通文字             => 模型这轮选择不调用。单次不调用说明不了什么，但
    **三次都不调用**就不是运气问题了 => FAIL（前言的引导力没被证实）。

第二次要量的东西，只有真模型能回答：**模型自己那一次的调用，客户端原样回显回来后，
前缀还粘得住吗？**

  模型刚吐出来时，记账用的是**原文**（parse_call_body 把参数原文留在 raw 里，渲染时
  优先用它）。但客户端回显走的是 OpenAI 形态——`arguments` 是一个 JSON 字符串，网关
  `normalize_tool_calls` 把它解成**有类型的 dict**，原文没了，只能按规范形重渲。两者
  逐字节相等，当且仅当模型吐的原文恰好是规范形：

      <parameter=city>北京</parameter>     -> 规范形，"北京"，相等
      <parameter=city>"北京"</parameter>   -> 规范形是 北京，**不等**（多一对引号）
      <parameter=days>1.50</parameter>     -> 规范形是 1.5，**不等**
      <parameter=on>true</parameter>       -> 规范形是 True，**不等**

  不等的话，前缀就断在助手轮上——不报错、答案也对，只是这个 Agent 之后每一轮全量
  重算。这正是 [[qwen35-27b-multisession-project]] 里那条"KV 复用不变量"要防的静默
  降级，而在 Agent 场景下它恰好是**每轮必踩**（每一轮历史里都躺着上一次的调用）。
  判据同 sticky_check.py：和冷会话对照，不用 `cached_tokens > 0`（前缀断在助手轮时，
  它前面的部分照样命中缓存）。

用法：python3 toolcall_check.py http://127.0.0.1:18280
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18280"
FAILS = []
NOTES = []
USED_CONV = []
PICKED_RIGHT = [0, 0]        # 挑对该用的工具的次数 / 有调用的探针数

# 调用标记。只认**具体的标签**，不认 "tool_call" 这个裸词——模型在正文里讨论这个
# 概念是常有的事，拿裸词当判据会造出假 FAIL。
MARKERS = ("<tool_call", "</tool_call", "<function=", "<parameter=")


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def info(msg):
    print("  [INFO] %s" % msg)


def note(msg):
    """观察，不是判据。

    "模型选中了该用的那个工具"属于**模型质量**，不属于我们跟客户端之间的契约：契约是
    "选中的工具在声明里、参数名在 properties 里、required 齐全"。把模型质量混进 FAILS
    会造出两类假红——桩上（固定回一个 get_weather）永远红，板上则是采样走偏就红一次。
    所以单列一栏：不算通过，也不算失败，但打印出来给人看。
    """
    print("  [NOTE] %s" % msg)
    NOTES.append(msg)


def close_conv(conv):
    """交还一个会话。

    会话是**稀缺资源**（板上只有 4 个），而这个脚本会开好几个。留着不还的话，后面的
    探针不是失败而是**排队**——排队等不到就 503，看起来像工具调用坏了，其实是我们自己
    把池子占满了。所以除了"回显往返要复用的那一个"，其余当场还。

    交还会失败（会话早就被 idle-ttl 收走了、或者压根没建立）——那种情况下服务端本来就
    没有它，不是错误，忽略。
    """
    body = json.dumps({"conversation_id": conv}).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/conversations/close", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except Exception:
        pass
    if conv in USED_CONV:
        USED_CONV.remove(conv)


def chat(messages, tools, conv, max_tokens=320):
    """一整轮非流式对话，回 (message, finish_reason, usage)。"""
    if conv not in USED_CONV:
        USED_CONV.append(conv)
    body = {"model": "qwen3.5-27b", "messages": messages, "max_tokens": max_tokens,
            "stream": False, "conversation_id": conv, "tools": tools}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.loads(r.read().decode("utf-8"))
    c = d["choices"][0]
    return c["message"], c.get("finish_reason"), d.get("usage") or {}


# 工具集里放**两个**不同用途的工具：只放一个的话，"模型选中了正确的那个"就退化成了
# "模型随便挑了一个"，验不出工具目录里的名字和描述有没有真的起作用。
TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "查询某个城市当前及未来几天的天气",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string", "description": "城市名"},
                                      "days": {"type": "integer",
                                               "description": "查未来几天，默认 1"}},
                       "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "发送一封邮件",
        "parameters": {"type": "object",
                       "properties": {"to": {"type": "string", "description": "收件人邮箱"},
                                      "subject": {"type": "string", "description": "邮件主题"}},
                       "required": ["to", "subject"]}}},
]

DECLARED = {t["function"]["name"]: t["function"] for t in TOOLS}

# 三次提问，都**必须**靠工具才能答（板卡天气、未来三天、发邮件——模型没有别的路子）。
# 措辞刻意不同：同一句话问三遍，模型答错也只是那一次采样走偏，验不出前言的稳健性。
PROBES = [
    ("p1-weather", "北京今天天气怎么样？", "get_weather"),
    ("p2-days", "帮我查一下上海未来三天的天气。", "get_weather"),
    ("p3-email", "给 zhangsan@example.com 发一封主题为「周报」的邮件。", "send_email"),
]

print("=== 真模型的工具调用核对：%s ===" % BASE)

def judge_call(name, calls, fin, want_tool):
    """验收一次落地的调用，返回它是否**可用于回显往返**（名字解得出来、参数解得开）。"""
    print("     调用：%s" % json.dumps(calls, ensure_ascii=False)[:300])
    check("[%s] 有 tool_calls 时 finish_reason == tool_calls" % name,
          fin == "tool_calls", "finish_reason=%r" % fin)

    fn = (calls[0].get("function") or {})
    got = fn.get("name")
    check("[%s] 选中的工具在声明里" % name,
          got in DECLARED, "选中=%r, 声明=%s" % (got, sorted(DECLARED)))
    if got not in DECLARED:
        return False
    PICKED_RIGHT[1] += 1
    if got == want_tool:
        PICKED_RIGHT[0] += 1
    else:
        note("[%s] 选中的是 %r，提问更该用 %r（模型质量，非契约；不算失败）"
             % (name, got, want_tool))

    try:
        args = json.loads(fn.get("arguments") or "{}")
        ok_json = isinstance(args, dict)
    except ValueError:
        args, ok_json = None, False
    check("[%s] arguments 是合法 JSON 且能解成对象" % name, ok_json,
          json.dumps(fn.get("arguments"), ensure_ascii=False)[:160])
    if not ok_json:
        return False

    spec = DECLARED[got].get("parameters") or {}
    props = spec.get("properties") or {}
    extra = sorted(set(args) - set(props))
    # 参数名不在声明里 = 工具目录没被当成目录读。值对不对是模型的事，名字是**我们**渲染
    # 的工具目录该管住的事，所以这条是硬判据。
    check("[%s] 参数名都在声明的 properties 里（目录被读懂了）" % name,
          not extra, "多出来的：%s；声明=%s" % (extra, sorted(props)))
    missing = [r for r in (spec.get("required") or []) if r not in args]
    check("[%s] required 参数齐全" % name, not missing, "缺：%s" % missing)
    # 值只打印不断言：模型省略带默认值的参数（days）是合理的，那不该判失败。
    print("     参数：%s" % json.dumps(args, ensure_ascii=False))
    return True


ok_probe = None          # (名字, message, conv)，第一个解出调用且能回显的探针
unparsed = []

for name, question, want_tool in PROBES:
    conv = "tcp-" + name
    print("== 探针 %s：%r ==" % (name, question))
    msg, fin, usage = chat([{"role": "user", "content": question}], TOOLS, conv)
    content = msg.get("content") or ""
    calls = msg.get("tool_calls") or []
    print("     finish_reason=%r 正文=%r" % (fin, content[:90].replace("\n", " ")))

    if not calls:
        hit = [m for m in MARKERS if m in content]
        if hit:
            # 模型说了，我们没听懂。把正文原样打出来——这段就是解析器该认而没认的形状。
            unparsed.append((name, content))
            print("  [FAIL] 正文里有调用标记 %s 但没解出 tool_calls（模型说了，我们没听懂）" % hit)
            print("         正文原文：%r" % content[:400])
        else:
            info("这一轮模型没有调用工具（正文是普通文字，不是解析失败）")
        usable = False
    else:
        usable = judge_call(name, calls, fin, want_tool)

    # 只有"要拿来回显往返的那一个"留着（粘性拴在它的会话上），其余当场交还。
    if usable and ok_probe is None:
        ok_probe = (name, msg, conv)
    else:
        close_conv(conv)

print("== 判定（可解性）==")
if ok_probe is None:
    check("三次探针里至少有一次解出工具调用", False,
          "模型一次都没调用——前言对模型的引导力未被证实；工具的**实现**仍由 "
          "serve_http_test.py tools 在桩上覆盖着")

# ---------------------------------------------------------------------------
# 回显往返：模型自己那一次的调用，客户端原样回显回来后前缀还粘得住吗？
# ---------------------------------------------------------------------------
if ok_probe is not None:
    name, msg, conv = ok_probe
    print("== 回显往返（用 %s 那一次真实的调用）==" % name)
    # assistant 消息**原样**用响应里的那份，就是任何 Agent 框架会回显的东西——不自己
    # 拼一遍，否则验的就不是客户端路径了。
    question = [q for n, q, w in PROBES if n == name][0]
    hist = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": msg.get("content") or "",
         "tool_calls": msg["tool_calls"]},
        {"role": "tool", "content": "北京 18 摄氏度，晴。"},
        {"role": "user", "content": "那明天呢？"},
    ]

    def recomputed(u):
        det = u.get("prompt_tokens_details") or {}
        return u.get("prompt_tokens", 0) - det.get("cached_tokens", 0), det.get("cached_tokens", 0)

    _, _, u_hot = chat(hist, TOOLS, conv, max_tokens=64)
    _, _, u_cold = chat(hist, TOOLS, conv + "-cold", max_tokens=64)
    hot, hot_cached = recomputed(u_hot)
    cold, _ = recomputed(u_cold)
    print("     热会话重算=%d tok（复用 %d）, 冷会话重算=%d tok" % (hot, hot_cached, cold))
    check("带模型自己那次调用的历史命中 KV 复用（本轮重算远小于冷会话）",
          hot * 2 < cold and hot_cached > 0,
          "热=%d vs 冷=%d" % (hot, cold))
    if hot * 2 >= cold:
        # 断在这儿只有一个可能的成因：模型吐的参数原文不是规范形，回显时被重渲成了
        # 另一个字节串。把参数打出来帮忙定位（带引号的字符串 / 1.50 / true 都是嫌疑）。
        print("  [诊断] 前缀断在助手轮上。可能的成因：模型吐的参数原文不是规范形，")
        print("         回显时按类型重渲成另一个字节串。该次调用的参数：")
        print("         %s" % json.dumps(msg["tool_calls"], ensure_ascii=False)[:400])
        print("         对照：serve_http_test.py tools 里**手写**的那段历史是规范的，")
        print("         它若在同一块板上通过，就说明渲染侧没问题，问题出在模型原文上。")

# 收尾把剩下的会话也交还（回显往返留下的那两个）。板上只有 4 个，而
# run_all_board_tests.sh 是一串脚本连着跑，留着不还的话后面的脚本（http_scaling 的
# N 路并发）会没有可用会话。
for _conv in list(USED_CONV):
    close_conv(_conv)

if PICKED_RIGHT[1]:
    note("选中该用的工具：%d/%d 次探针" % (PICKED_RIGHT[0], PICKED_RIGHT[1]))

print("\n%s" % ("全部通过" if not FAILS else "失败项：%s" % ", ".join(FAILS)))
if NOTES:
    print("观察项（不影响判定）：")
    for n in NOTES:
        print("  · %s" % n)
sys.exit(0 if not FAILS else 1)
