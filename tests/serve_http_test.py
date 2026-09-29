#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关的 HTTP 客户端测试（只用标准库——板卡上没有 curl，也没有 requests）。

先在本地对着桩后端跑，再到板卡上对着真后端跑同一份：
    python serve_http_test.py http://127.0.0.1:18280            # 全量
    python serve_http_test.py http://127.0.0.1:18280 quick      # 只跑 health/models/chat

关键的一项是多轮 KV 复用的 HTTP 级证据：同一段对话的第二轮，网关只发差异部分，
后端回报的 prefill token 数应当**明显小于**把整段 prompt 重发一遍的那次。
usage.prompt_tokens 就是这个数，所以从 HTTP 这一层就能看见复用有没有生效。

**为什么每个请求都带 conversation_id（v1.9 起）**：网关现在**只排队、不抢占**——一段
新对话拿不到空闲会话时是等，不是把别人占着的会话夺过来。所以"每问一个新问题就开一段
新对话"这种写法，在会话数（默认 4）用完之后会一路排队到 `IDLE_TTL` 才动。
本脚本因此给每个 section 显式命名对话、用完就 `close`，全程占用的会话数是有界的
（最多 N 路并发那一段）。这也正是多人接入时该有的写法：**身份要显式给，离开要显式说。**
"""
import json
import sys
import time
import threading
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18280"
MODE = sys.argv[2] if len(sys.argv) > 2 else "full"
#   full    —— 默认：接口面 + 多轮 KV 复用 + 并发（run_all_board_tests.sh 跑这个）
#   quick   —— 只跑到流式为止
#   reject  —— 后端拒了一轮（上下文装不下）时的 HTTP 语义；需 `--ctx-limit` 桩
#   bigmax  —— 离谱的 max_tokens 不得打死会话；需实现了那条校验的桩（见 fake_backend）
#   tools   —— 工具调用：注入工具说明 -> 生成 -> 摘出调用 -> OpenAI 形态，以及工具结果
#              回灌。桩上跑（fake_backend.py --tool-call）覆盖最全；板上也跑得动，但
#              "模型这轮会不会真调用"取决于模型意愿，不调用时那几条打 INFO 而不是 FAIL
TIMEOUT = 1800

OK = [True]


def check(name, cond, detail=""):
    OK[0] = OK[0] and bool(cond)
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path, body, stream=False, conv=None):
    headers = {"Content-Type": "application/json"}
    if conv:
        headers["X-Conversation-Id"] = conv
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers=headers)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def close_conv(conv):
    """告诉网关"这段对话结束了"，把会话立刻还回去（不等 IDLE_TTL）。

    这是 v1.9 新增的端点。**没有它，这个套件在会话数 ≤4 的板卡上会自己把自己堵死**：
    下面每个 section 都要各占几个会话，谁都不放的话第 5 个 section 只能排队。
    """
    with post("/v1/conversations/close", {"conversation_id": conv}) as r:
        return json.loads(r.read().decode("utf-8"))


def close_anonymous():
    """把套件自己造出来的**匿名**对话清掉（它们没有名字，只能按 `key` 关）。

    为什么必须清：匿名对话的身份是网关按内容算的摘要，客户端算不出来，所以它**关不掉也
    认不出**，会一直占着一个会话到 `IDLE_TTL` 超时。留着不管的话，套件跑第二遍时可用
    会话就少一个，N 路并发那一段会有一路拿不到会话、排队超时失败——**第一遍全绿、第二遍
    失败**，是最容易查错方向的那种现象。
    """
    _, pool = get("/v1/pool")
    closed = []
    for slot in pool.get("slots", []):
        key = slot.get("key")
        if key and key.startswith("h:"):
            with post("/v1/conversations/close", {"key": key}) as r:
                closed.append(json.loads(r.read().decode("utf-8")))
    return closed


def chat(messages, max_tokens=64, stream=False, conv=None, **extra):
    body = {"model": "qwen3.5-27b", "messages": messages,
            "max_tokens": max_tokens, "stream": stream}
    body.update(extra)
    if not stream:
        with post("/v1/chat/completions", body, conv=conv) as r:
            return json.loads(r.read().decode("utf-8"))
    chunks = []
    text = []
    with post("/v1/chat/completions", body, conv=conv) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunks.append(json.loads(payload))
            delta = chunks[-1]["choices"][0].get("delta", {}) if chunks[-1].get("choices") else {}
            if delta.get("content"):
                text.append(delta["content"])
    return chunks, "".join(text)


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查某城市当前天气",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string", "description": "城市名"},
                                      "days": {"type": "integer", "description": "查几天"}},
                       "required": ["city"]},
    },
}


def tool_section():
    """工具调用（function calling）在 HTTP 这一层的验收。

    **两段性质不同的检查**，别把第二段当成第一段的替代：
      · 「模型会不会真调用」取决于模型意愿，桩上（`--tool-call`）必然发生、板上不一定。
        所以这里只在**真拿到** tool_calls 时断言契约（finish_reason / arguments 是合法
        JSON / id 和 type），没拿到就打一行显式的 INFO——不把"模型这轮没调"伪装成通过。
      · 「回灌 tool 结果再问一轮」是**确定的**：助手轮带 tool_calls 的历史由我们手写，
        不依赖模型意愿。它端到端验的是渲染侧最要命的那条（连续 tool 消息合成一个 user
        轮 + 助手轮调用块），板上和桩上都会跑到。
    """
    print("== 工具调用：注入 -> 生成 -> 摘出 -> OpenAI 形态 ==")
    ask = [{"role": "user", "content": "北京今天天气怎么样？"}]
    d = chat(ask, max_tokens=320, conv="probe-tool", tools=[WEATHER_TOOL])
    msg = d["choices"][0]["message"]
    fin = d["choices"][0]["finish_reason"]
    calls = msg.get("tool_calls") or []
    if calls:
        check("有 tool_calls 时 finish_reason == tool_calls",
              fin == "tool_calls", "finish_reason=%r" % fin)
        check("tool_calls 的形态是 OpenAI 那种（id/type/function.name/arguments 齐全）",
              all(c.get("id") and c.get("type") == "function"
                  and c.get("function", {}).get("name")
                  and isinstance(c["function"].get("arguments"), str) for c in calls),
              json.dumps(calls, ensure_ascii=False)[:200])
        # arguments 必须是**能解出来的 JSON 字符串**，不是把原文直接塞进去。客户端要按
        # JSON 去解它，解不开的工具调用等于没调用。
        try:
            args0 = json.loads(calls[0]["function"]["arguments"])
            ok_json = isinstance(args0, dict)
        except ValueError:
            args0, ok_json = None, False
        check("arguments 是合法 JSON 且能解成对象", ok_json,
              json.dumps(calls[0]["function"]["arguments"], ensure_ascii=False)[:120])
        check("正文里不再残留 <tool_call> 标签（调用已经被摘出去了）",
              "<tool_call>" not in (msg.get("content") or ""),
              "content=%r" % (msg.get("content") or "")[:80])
    else:
        # 板上模型这轮可能选择不调用（比如它觉得自己知道答案）。这**不是**失败，但也
        # 不是通过——写清楚，免得把一次没覆盖到的跑批当成覆盖到了。
        print("  [INFO] 这一轮模型没有调用工具（finish_reason=%r, content=%r）"
              % (fin, (msg.get("content") or "")[:60].replace("\n", " ")))
        print("         桩上（fake_backend.py --tool-call）必然发生；板上取决于模型意愿。")
    close_conv("probe-tool")

    print("== 工具结果回灌：助手轮带 tool_calls 的历史（确定路径，不依赖模型意愿）==")
    # 手写一段"已经调过一次"的历史。它端到端验的是渲染侧：助手轮的调用块要按模板渲成
    # <tool_call>/<function=...>，tool 角色要渲成 <tool_response> 包在一个 user 轮里
    # （模板里根本没有 tool 轮，渲成 <|im_start|>tool 是改造前的错法）。
    hist = [
        {"role": "user", "content": "北京今天天气怎么样？"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_0", "type": "function",
             "function": {"name": "get_weather",
                          "arguments": json.dumps({"city": "北京", "days": 1},
                                                  ensure_ascii=False)}}]},
        {"role": "tool", "content": "北京 18 摄氏度，晴。"},
        {"role": "tool", "content": "湿度 40%。"},          # 连续两条 -> 合成一个 user 轮
        {"role": "user", "content": "那明天呢？"},
    ]
    d2 = chat(hist, max_tokens=320, conv="probe-tool", tools=[WEATHER_TOOL])
    m2 = d2["choices"][0]["message"]
    c2 = m2.get("content") or ""
    c2_calls = m2.get("tool_calls") or []
    # 这一轮真模型大概率**又调一次工具**（2026-09-17 板上实测如此），所以判据
    # 不能是"正文非空"：那种回复的正文只有一对空的 <think></think>，甚至可能是空的。
    check("带 tool_calls + 连续两条 tool 结果的历史能正常出一轮",
          bool(c2.strip()) or bool(c2_calls),
          "正文=%r 调用=%d 个" % (c2[:40].replace("\n", " "), len(c2_calls)))

    print("== 工具循环里的 KV 复用：带工具的历史同样要粘得住 ==")
    # 这一条才是前面所有字节级较真的**目的**。工具轮的历史（助手调用块 + <tool_response>）
    # 只要渲染出来的字节和上一轮实际发出去的差一点，前缀就断在那儿、之后每轮全量重算——
    # 不报错、答案也对，只在这里现形。
    #
    # **下一轮的助手消息必须原样带上这一轮的 tool_calls**。这不只是"像 Agent 那样"：
    # 网关的判据是 `prompt.startswith(known[session])`（全匹配，不做最长公共前缀的部分
    # 复用，见 rkllm_gateway.py 的 _make_lease），所以助手轮少一个字段，代价是**整段
    # prefill 重算**、`cached_tokens` 直接归零——2026-09-17 真机上就是这么红的：桩在
    # 这一轮不吐调用，于是 `content=c2` 恰好够用；真模型吐了调用，只回显正文就把调用丢了。
    # 拿"模型这一轮没调用"当前提的测试，桩上过、板上挂。
    tail = [{"role": "assistant", "content": c2}]
    if c2_calls:
        tail[0]["tool_calls"] = c2_calls
        tail.append({"role": "tool", "content": "明天 20 摄氏度，多云。"})
    tail.append({"role": "user", "content": "谢谢，那后天呢？"})
    follow = hist + tail

    # **判据必须是和冷会话的对照，不能是 `cached_tokens > 0`**：这条判据在改造期间真的
    # 空转过——桩上把记账文本多拼一个空格（前缀必断），cached 从 567/584 掉到 44/443，
    # `> 0` 照样通过。所以比的是"本轮真正重算的部分"：热会话 vs 换个身份的冷会话
    # （没有粘性记录）。同 sticky_check.py 的 `(p_s - c_s) * 2 < p_f`。

    def recomputed(d):
        u = d["usage"]
        return (u["prompt_tokens"] - (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))

    d3 = chat(follow, max_tokens=64, conv="probe-tool", tools=[WEATHER_TOOL])
    d4 = chat(follow, max_tokens=64, conv="probe-tool-cold", tools=[WEATHER_TOOL])
    hot, hot_cached = recomputed(d3)
    cold, _ = recomputed(d4)
    check("带工具的多轮历史命中 KV 复用（本轮重算部分远小于冷会话）",
          hot * 2 < cold and hot_cached > 0,
          "热会话重算=%d tok（复用 %d）, 冷会话重算=%d tok" % (hot, hot_cached, cold))
    close_conv("probe-tool-cold")

    print("== 流式 + 工具：增量协议也要能带 tool_calls ==")
    chunks, text = chat(ask, max_tokens=320, stream=True, conv="probe-tool",
                        tools=[WEATHER_TOOL])
    fins = [c["choices"][0].get("finish_reason") for c in chunks if c.get("choices")]
    tcs = [tc for c in chunks if c.get("choices")
           for tc in (c["choices"][0].get("delta") or {}).get("tool_calls") or []]
    check("流式路径有收尾的 finish_reason", any(f is not None for f in fins),
          "finish=%s" % [f for f in fins if f is not None])
    if tcs:
        check("流式 finish_reason == tool_calls", fins[-1] == "tool_calls",
              "finish=%r" % fins[-1])
        check("流式 tool_calls 增量带 index（客户端靠它拼同一路调用）",
              all("index" in tc for tc in tcs),
              json.dumps(tcs, ensure_ascii=False)[:200])
        # 流式和非流式给客户端的**正文**必须一致：不一致的话客户端回显流式那份再发
        # 下一轮，渲染出来的字节就和记账的对不上，粘性复用静默退化成每轮全量重算。
        # （正文的规范化只在渲染侧做一次，见 toolcalls.render_assistant_turn。）
        check("流式正文里也没有残留的 <tool_call> 标签", "<tool_call>" not in text,
              "正文=%r" % text[:80])
    else:
        print("  [INFO] 流式这一轮没有工具调用增量（同上一段的说明）。")
    close_conv("probe-tool")


def main():
    print("=== 网关 HTTP 测试：%s ===" % BASE)

    print("== health / models ==")
    st, health = get("/health")
    check("health 200", st == 200, json.dumps(health, ensure_ascii=False))
    st, models = get("/v1/models")
    mid = models["data"][0]["id"]
    check("models 里有条目", bool(mid), "model id = %s" % mid)

    print("== 非流式 ==")
    t0 = time.time()
    d = chat([{"role": "user", "content": "用一句话说明你是谁。"}], max_tokens=48,
             conv="probe-oneshot")
    dt = time.time() - t0
    msg = d["choices"][0]["message"]
    check("有 content", len(msg.get("content") or "") > 0,
          "%.1fs, %d 字, finish=%s" % (dt, len(msg.get("content") or ""),
                                       d["choices"][0]["finish_reason"]))
    check("usage 有 prompt/completion",
          d["usage"]["prompt_tokens"] > 0 and d["usage"]["completion_tokens"] > 0,
          json.dumps(d["usage"]))
    close_conv("probe-oneshot")     # 用完就还，见文件头"为什么每个请求都带 conversation_id"

    print("== 流式 SSE ==")
    chunks, text = chat([{"role": "user", "content": "数到五。"}], max_tokens=32,
                        stream=True, conv="probe-oneshot")
    deltas = [c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("content")]
    check("收到多个 delta", len(deltas) > 1, "%d 个" % len(deltas))
    check("首块带 role", any(c.get("choices") and c["choices"][0]["delta"].get("role") == "assistant"
                             for c in chunks))
    check("有 finish_reason", any(c.get("choices") and c["choices"][0].get("finish_reason")
                                  for c in chunks))
    check("拼出的文本非空", len(text) > 0, "%d 字" % len(text))
    close_conv("probe-oneshot")     # 见文件头：不还回去，后面几段就会为会话数打架

    if MODE == "tools":
        # 早退：这段是自成一体的，没必要把后面的并发/伸缩也跑一遍（几分钟起）。
        tool_section()
        print("\n%s" % ("全部通过" if OK[0] else "有失败项"))
        return 0 if OK[0] else 1

    if MODE == "quick":
        print("\n%s" % ("全部通过" if OK[0] else "有失败项"))
        return 0 if OK[0] else 1

    if MODE == "reject":
        # 只跑"后端拒了一轮"这一段。**需要网关后面挂的是带 `--ctx-limit N` 的桩后端**
        # （真后端没这个开关，真板上要把上下文撑到 4096 token 才触发）：
        #   python rkllm_gateway.py --port 8098 --sessions 2 --frames-stdout \
        #       -- python fake_backend.py --sessions 2 --ctx-limit 500
        #   python serve_http_test.py http://127.0.0.1:8098 reject
        # 验的是协议层之外**只有 HTTP 层才看得见**的两件事：状态码（400，不是 503——
        # 503 会诱导客户端原样重试，而原样重试必然再被拒一次）和会话没被标死。
        print("== 后端拒了一轮（上下文装不下）=> HTTP 400，且会话不被标死 ==")
        conv = "probe-reject"
        filler = "记住暗号：青柠味苏打水。"
        d1 = chat([{"role": "user", "content": filler}], max_tokens=16, conv=conv)
        a1 = d1["choices"][0]["message"]["content"]
        check("第一轮正常跑完（还没到上限）", len(a1) > 0)
        hist = [{"role": "user", "content": filler},
                {"role": "assistant", "content": a1}]
        status, body = None, ""
        try:
            chat(hist + [{"role": "user", "content": "再说一遍暗号。"}],
                 max_tokens=16, conv=conv)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8", "replace")
        check("被拒的那轮回 400（客户端该改请求，不是重试）", status == 400,
              "HTTP %s %s" % (status, body[:120]))
        check("错误类型是 invalid_request_error（不是 server_error）",
              "invalid_request_error" in body, body[:200])
        _, pool = get("/v1/pool")
        states = [s.get("state") for s in pool.get("slots", [])]
        check("会话没被标死（池子里没有 dead 状态）", "dead" not in states,
              "states=%s" % states)
        # 会话还在池子里不够，还要能真的继续服务：被拒之后同一段对话换成新的一轮，
        # 应当照常拿到回答（KNOWN=None => RESET 全量重发，这一步顺带验了自愈）。
        d3 = chat([{"role": "user", "content": "全新的一轮。"}], max_tokens=16,
                  conv=conv)
        check("被拒之后这段对话还能继续服务", len(d3["choices"][0]["message"]["content"]) > 0)
        close_conv(conv)
        print("\n%s" % ("全部通过" if OK[0] else "有失败项"))
        return 0 if OK[0] else 1

    if MODE == "bigmax":
        # 客户端写一个离谱的 max_tokens（`1e12` 是"能吐多少吐多少"很常见的写法）时，
        # 服务必须照常给答案、**不能因此丢会话**。
        #
        # 这条守的是一条真实的可达路径：max_new_tokens 在帧协议的字段最终落到后端的
        # `int` 上，后端 2026-09-16 起会拒收超出 int32 的值，而 ERR 帧的语义是"这个
        # 会话的驱动线程没了"——网关收到就把会话标死。所以如果网关不把它夹进范围，
        # **一个客户端随口写的数字就能永久吃掉一个会话**，四次之后整个服务没有会话可用。
        # 夹的范围在 `MAX_NEW_TOKENS_CAP`（网关侧）+ 后端/桩的同一条校验，三层对齐。
        #
        # 需要桩实现那条校验才测得出来（`fake_backend.py` 已实现），所以这一条要在
        # 桩后端上跑：装了不带校验的桩，它反而会照常答完、把缺陷盖住。
        print("== 离谱的 max_tokens 不能打死会话 ==")
        conv = "probe-bigmax"
        # 未修复时这里回的是 **503**（会话被标死），所以第一个请求要自己接住 HTTPError
        # 并且**如实报成 FAIL**：让脚本崩出 traceback 虽然也会被判失败，但看不出是哪一个
        # 缺陷——而这一段的失败信息必须直接指出"是一个客户端的数字吃掉了会话"。
        status, err, d = None, "", None
        try:
            d = chat([{"role": "user", "content": "只回复两个字：收到。"}],
                     max_tokens=10 ** 12, conv=conv)
        except urllib.error.HTTPError as exc:
            status = exc.code
            err = exc.read().decode("utf-8", "replace")
        check("照常回 200（不是 503）", status is None,
              "HTTP %s %s" % (status, err[:160]) if status is not None
              else json.dumps(d["usage"]))
        check("有内容", d is not None and len(d["choices"][0]["message"]["content"]) > 0)
        _, pool = get("/v1/pool")
        states = [s.get("state") for s in pool.get("slots", [])]
        check("没有会话被标死（池子里没有 dead 状态）", "dead" not in states,
              "states=%s" % states)
        ok2 = False
        try:
            d2 = chat([{"role": "user", "content": "再来一轮。"}], max_tokens=16, conv=conv)
            ok2 = len(d2["choices"][0]["message"]["content"]) > 0
        except urllib.error.HTTPError as exc:
            print("      （续跑的请求也失败了：HTTP %s）" % exc.code)
        check("这段对话还能继续服务", ok2)
        close_conv(conv)
        print("\n%s" % ("全部通过" if OK[0] else "有失败项"))
        return 0 if OK[0] else 1

    print("== 多轮 KV 复用（HTTP 级证据：usage.prompt_tokens_details.cached_tokens）==")

    def kv_reuse_case(label, conv, extra):
        # 关思考和开思考要各测一遍。关思考时网关会把 think 段从正文里摘掉，于是"记进
        # 会话账本的文本"和"模型实际生成的原始文本"不再逐字节相同——如果记账用了原始
        # 版，客户端下一轮回显的（已摘标签的）历史就对不上前缀，复用每轮都会退化成全量。
        # 这个坑答案完全正确、只有速度变差，所以必须靠 usage 里的复用计数抓。
        #
        # 判据在 2026-09-16 换过一次：以前看的是 "prompt_tokens 比全量小"，那是把
        # prompt_tokens 当"本轮 prefill 的增量"用的**非标准口径**——而 OpenAI 里它是
        # 整段 prompt 的长度。网关已改成标准口径（prompt_tokens = 整段，复用部分单列
        # cached_tokens），于是旧判据必然失败：续聊的整段 prompt 本来就比全新对话长。
        # 现在直接看复用计数：cached_tokens > 0，且本轮真正重算的部分远小于全量。
        filler = "请记住这句话：" + "麒麟九千" * 40
        d1 = chat([{"role": "user", "content": filler}], max_tokens=16, conv=conv, **extra)
        a1 = d1["choices"][0]["message"]["content"]
        hist = [{"role": "user", "content": filler},
                {"role": "assistant", "content": a1}]
        d2 = chat(hist + [{"role": "user", "content": "继续。"}], max_tokens=16,
                  conv=conv, **extra)
        # 另起一段对话（换个首条 user 内容）走全量 prefill 作为对照
        d3 = chat([{"role": "user", "content": "换一段全新的对话：" + "麒麟九千" * 40}],
                  max_tokens=16, conv=conv + "-fresh", **extra)
        p2 = d2["usage"]["prompt_tokens"]
        cached2 = d2["usage"]["prompt_tokens_details"]["cached_tokens"]
        recomputed = p2 - cached2            # 本轮真正 prefill 的那部分
        p3 = d3["usage"]["prompt_tokens"]    # 对照那轮没有可复用的前缀，重算 = 全量
        check("续聊命中了 KV 复用（cached_tokens > 0）[%s]" % label, cached2 > 0,
              "续聊 prompt=%d tok，其中复用 %d tok，本轮只算了 %d tok"
              % (p2, cached2, recomputed))
        check("真正重算的部分远小于全量[%s]" % label, recomputed < p3,
              "续聊重算=%d tok, 全量 prefill=%d tok, 比值 %.2f"
              % (recomputed, p3, (recomputed / p3 if p3 else 0)))
        close_conv(conv)
        close_conv(conv + "-fresh")

    kv_reuse_case("开思考", "probe-reuse-think", {})
    kv_reuse_case("关思考", "probe-reuse-nothink",
                  {"chat_template_kw": {"enable_thinking": False}})

    print("== 并发（N 路同时打，聚合吞吐）==")
    # 并发这一段**必须**是 N 段不同的对话：同一段对话的两轮会被钉在同一个会话上，
    # 那样量到的是"排队"而不是"并发"。每一路各给一个身份。
    n = 4
    results = [None] * n

    def worker(i):
        try:
            t = time.time()
            dd = chat([{"role": "user", "content": "第 %d 路：数到二十。" % i}], max_tokens=64,
                      conv="probe-n%d" % i)
            results[i] = (time.time() - t, dd["usage"]["completion_tokens"])
        except Exception as exc:                       # noqa: BLE001
            results[i] = (-1, "FAILED: %r" % (exc,))

    t0 = time.time()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    tot = sum(r[1] for r in results if r and isinstance(r[1], int))
    check("N 路都成功", all(r and isinstance(r[1], int) for r in results),
          "各耗时 " + ", ".join("%.1fs" % r[0] for r in results if r))
    check("聚合吞吐 > 单路", tot / wall > 0, "合计 %d tok / %.1fs = %.2f tok/s"
          % (tot, wall, tot / wall if wall else 0))
    for i in range(n):
        close_conv("probe-n%d" % i)

    print("== 关思考（chat_template_kw）==")
    d4 = chat([{"role": "user", "content": "1+1=?"}], max_tokens=64, conv="probe-oneshot",
              chat_template_kw={"enable_thinking": False})
    c4 = d4["choices"][0]["message"]["content"]
    check("返回非空", len(c4) > 0, "前 60 字：%s" % c4[:60].replace("\n", " "))
    # 关思考是靠 /no_think 软开关，模型照样会吐一个（空的）<think> 段；网关负责摘掉，
    # 否则 Agent 拿到的正文里带着标签，还得自己洗一遍。
    check("正文里没有残留的 <think> 标签", "<think>" not in c4 and "</think>" not in c4,
          "前 60 字：%s" % c4[:60].replace("\n", " "))
    _, c5 = chat([{"role": "user", "content": "1+1=?"}], max_tokens=64, stream=True,
                 conv="probe-oneshot", chat_template_kw={"enable_thinking": False})
    check("流式路径也没有残留的 <think> 标签",
          "<think>" not in c5 and "</think>" not in c5, "前 60 字：%s" % c5[:60].replace("\n", " "))
    d6 = chat([{"role": "user", "content": "1+1=?"}], max_tokens=64, conv="probe-oneshot")
    c6 = d6["choices"][0]["message"]["content"]
    check("开思考（默认）时原样透传，不擅自丢推理过程", "<think>" in c6,
          "前 40 字：%s" % c6[:40].replace("\n", " "))
    close_conv("probe-oneshot")

    nsess = int(health.get("sessions") or 0)
    print("== 多用户身份（会话按身份分，不按内容）==  sessions=%d" % nsess)
    ident = min(3, max(0, nsess))
    if ident < 2:
        check("要有 ≥2 个会话才能验身份分流", False, "sessions=%d" % nsess)
    else:
        # **同一句话 + 不同身份 => 不同会话**。这是多人接入的底线：靠内容认对话的话，
        # 两个人问同一句话会被当成同一段对话、钉在同一个会话上串行（答案全对、吞吐减半）。
        probe = "身份分流探针：只回复 ok。"
        sids = []
        for i in range(ident):
            try:
                with post("/v1/chat/completions",
                          {"model": "qwen3.5-27b",
                           "messages": [{"role": "user", "content": probe}],
                           "max_tokens": 8}, conv="probe-id%d" % i) as r:
                    kv = r.headers.get("X-KV-Reuse") or ""
                    r.read()
            except Exception as exc:                   # noqa: BLE001
                check("身份分流请求全部成功", False, "probe-id%d: %r" % (i, exc))
                sids = []
                break
            sids.append(dict(p.split("=") for p in kv.split(";") if "=" in p)
                        .get("session", "").strip())
        if sids:
            check("同一句话、不同身份 => 落在不同会话上", len(set(sids)) == ident,
                  "落到会话 %s（%d 个身份）" % (sids, ident))
        for i in range(ident):
            close_conv("probe-id%d" % i)

        # 反向对照：**不给身份**，同一句话就只能按内容认 => 全落到同一个会话上。
        # 这条是"为什么必须显式给身份"的实证，不是理论说明。
        sids2 = []
        for _ in range(ident):
            with post("/v1/chat/completions",
                      {"model": "qwen3.5-27b",
                       "messages": [{"role": "user", "content": probe}],
                       "max_tokens": 8}) as r:
                kv = r.headers.get("X-KV-Reuse") or ""
                r.read()
            sids2.append(dict(p.split("=") for p in kv.split(";") if "=" in p)
                         .get("session", "").strip())
        check("不给身份 => 同一句话全落到同一个会话（正是要避免的退化）",
              len(set(sids2)) == 1, "落到会话 %s" % (sids2,))
        # 这一段的匿名对话没有名字（身份是网关按内容算的摘要，客户端算不出），所以只能
        # 按 `GET /v1/pool` 里的原始 `key` 关。顺手证明这条路是通的——没有它，套件跑
        # 第二遍时可用会话会少一个，N 路并发那一段就会莫名其妙地失败。
        anon = close_anonymous()
        check("匿名对话能按 /v1/pool 的 key 关掉（否则会一直占到 IDLE_TTL）",
              anon and all(c.get("closed") for c in anon),
              "关掉 %d 段匿名对话" % len(anon))

    print("== 会话池：排队不抢占 + 主动交还 ==")
    st, pool = get("/v1/pool")
    check("/v1/pool 可读且槽位数 = 会话数",
          st == 200 and len(pool.get("slots", [])) == nsess,
          "slots=%d waiting=%d reaped=%d idle_ttl=%ss"
          % (len(pool.get("slots", [])), len(pool.get("waiting", [])),
             pool.get("reaped_total", 0), pool.get("idle_ttl_s")))
    # 第二级阈值必须在快照里。只看 `/v1/pool` 的人要能算出"我这个请求最坏等几秒"；
    # 2026-09-18 那次"NPU 没人用却等 226s"就是因为屏幕上和快照里都看不出这一级的存在。
    check("/v1/pool 报出了 contend_idle（有人在等时的那一级阈值）",
          "contend_idle_s" in pool,
          "contend_idle=%ss（idle_ttl=%ss）" % (pool.get("contend_idle_s"),
                                                pool.get("idle_ttl_s")))
    closed = close_conv("probe-never-existed")
    check("close 一段不存在的对话是幂等的（不报错）",
          closed.get("closed") is False and closed.get("session") is None,
          json.dumps(closed, ensure_ascii=False))
    try:
        post("/v1/conversations/close", {}).read()
        check("close 不带身份 => 400", False)
    except urllib.error.HTTPError as exc:
        check("close 不带身份 => 400（匿名对话关不掉，因为它的 id 是内容摘要）",
              exc.code == 400, "HTTP %d" % exc.code)

    print("\n%s" % ("全部通过" if OK[0] else "有失败项"))
    return 0 if OK[0] else 1


if __name__ == "__main__":
    sys.exit(main())
