#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具调用（function calling）：prompt 侧的渲染 + 回复侧的解析。

**这个文件里的格式不是猜的。** 板卡上 Qwen3.5-27B 的工具调用形态取自它自己的
`tokenizer.chat_template`（GGUF 元数据键 `tokenizer.chat_template`，提取脚本见
`rt_work/gguf_tmpl.py`）。这一代是 **XML 形式**，不是 Qwen3 早期那种 Hermes JSON：

    <tool_call>
    <function=get_weather>
    <parameter=city>
    北京
    </parameter>
    </function>
    </tool_call>

（Hermes 的 `<tool_call>{"name":...,"arguments":{...}}</tool_call>` 在**解析**侧也顺手
认了——只在模型偶尔回退到那个形态时兜底，不指望它；**渲染**侧一律用 XML。）

system 消息里那段工具说明逐字照抄模板，见 `_TOOLS_PROLOGUE`。三个容易写错的点：
  · 工具定义用 Jinja 的 `tojson`：**键排序**、`ensure_ascii=True`（中文变 \\uXXXX）、
    并做 HTML 转义（`<` `>` `&` `'` → \\u003c 等）。看着别扭，但模型是按这个训练的。
  · 助手轮里第一条 tool_call 前面的分隔是 `\\n\\n`（仅当正文非空），后续每条是 `\\n`。
  · **连续的 tool 消息合成一个 user 轮**，每条各自包一层 <tool_response>。
    （原来的实现把 tool 角色渲成 `<|im_start|>tool`，那是错的——模板里根本没有 tool 轮。）

**与模板的刻意偏差**，都是为了 KV 复用（见 rkllm_gateway.render_messages 顶部那段）。
2026-09-18 之后只剩**一条**，而且只在**思考开着**的时候才存在：
  1. 助手轮在历史里**原样保留**模型当时生成的那段文本，不做模板那套
     「把 <think> 段拆出来重新包成 <think>\\n{推理}\\n</think>\\n\\n」的改写。
     原因：改写 = 下一轮 prompt 的前缀对不上 = KV 复用全废，而"只慢不错"看不出来。
     实测里粘性比这点格式保真度值钱（长上下文下是全量重算 vs 只发差异的区别）。
     代价：多步工具循环里模型看到的助手轮少了 <think> 包装，属于已知偏差。
     **关思考下这一条已经不存在**——那边包装里本来就是空的（一对空标记），渲得出来。
  2. （原第 2 条「`add_generation_prompt` 不带模板里的 `<think>\\n`」**已作废**，
     2026-09-18 补回。）作废的理由值得留着：`/no_think` 只是**软**提示，挡不住
     "要组织语言"的题——板上实测 `介绍一下杭州` 会写满一整段
     `<think>Thinking Process:…`，`max_tokens` 一截、`</think>` 来不及吐，网关又按
     "宁可露标签也不静默吞内容"原样透传，网页那一格就是一堵英文推理墙。现在关思考时
     按模板补 `<think>\\n\\n</think>\\n\\n`，模型从 `</think>` 后面开始生成，
     **结构上开不了** think 段。代价：关思考下 prompt 长度与 `completion_tokens` 都变了，
     **2026-09-18 之前量到的吞吐数字不能与之后的逐字节互比**——这正是当初不敢改它的
     那条理由，现在由用户拍板接受。思考开着的那一支仍然不带 `<think>\\n`，所以
     `check_template.py` 的金标准比对里，et=None 的用例照旧把那截尾巴显式换掉。

`_TOOLS_PROLOGUE` 的正确性由 `check_template.py` 逐字节对拍保证（拿真 Jinja2 渲模型
自己的模板，不靠人眼）。
"""
import json
import re

# 模板里 tools 在场时，system 消息正文的开头这一段（到 <tools> 的行为止）。
_TOOLS_HEAD = "# Tools\n\nYou have access to the following functions:\n\n<tools>"

# 工具清单之后、system 正文之前的那一大段说明。逐字来自模板（去掉 Jinja 的转义）。
_TOOLS_TAIL = (
    "\n</tools>"
    "\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:"
    "\n\n<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\n"
    "value_1\n</parameter>\n<parameter=example_parameter_2>\n"
    "This is the value for the second parameter\nthat can span\nmultiple lines\n</parameter>\n"
    "</function>\n</tool_call>"
    "\n\n<IMPORTANT>\nReminder:\n"
    "- Function calls MUST follow the specified format: an inner <function=...></function> "
    "block must be nested within <tool_call></tool_call> XML tags\n"
    "- Required parameters MUST be specified\n"
    "- You may provide optional reasoning for your function call in natural language BEFORE "
    "the function call, but NOT after\n"
    "- If there is no function call available, answer the question like normal with your "
    "current knowledge and do not tell the user about function calls\n"
    "</IMPORTANT>"
)

MARK_OPEN = "<tool_call>"
MARK_CLOSE = "</tool_call>"


def jinja_tojson(obj):
    """复刻 Jinja2 `|tojson` 的行为（模板里工具定义就是这么渲的）。

    Jinja2 的 `htmlsafe_json_dumps` 默认策略是 `sort_keys=True` + `json.dumps` 自己的
    `ensure_ascii=True`，再对 `<` `>` `&` `'` 做 HTML 转义（`'` 是因为它可能落在
    HTML 属性里）。**键排序和 ASCII 转义看着都是多余的**——这份文本只喂给模型、不进
    HTML——但板卡上的模型就是按这个形态训练的，多写五行换回分布内输入，划算。
    """
    s = json.dumps(obj, sort_keys=True, ensure_ascii=True)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("&", "\\u0026").replace("'", "\\u0027"))


def render_tools_system(tools, system_content=""):
    """模板里 tools 在场时那条 system 消息的**正文**（不含 `<|im_start|>system\\n` 与
    `<|im_end|>`）。

    模板把工具说明放在**系统人设之前**，中间空一行；人设为空则整段不出现。
    """
    body = [_TOOLS_HEAD]
    for tool in tools:
        body.append("\n" + jinja_tojson(tool))
    body.append(_TOOLS_TAIL)
    text = "".join(body)
    if system_content:
        text += "\n\n" + system_content
    return text


def tool_response_block(content):
    """一条 tool 消息的样子。

    **不带前导换行**：换行由角色行统一给（`<|im_start|>user\\n` + 块），多条块之间用
    `\\n` 相连。块自带前导 `\\n` 再加连接符会多出一个空行——对拍时抓到过，模型收到的
    形状就变了。
    """
    return "<tool_response>\n%s\n</tool_response>" % content


def _param_text(value):
    """参数值渲成文本。模板的规则：字符串原样，容器走 tojson，其余走 `|string`。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def normalize_tool_calls(raw):
    """OpenAI 形态（`function.arguments` 是 JSON **字符串**）-> 内部形态（arguments 是 dict）。

    客户端回显的助手消息走这条路。参数解析不了时保留 name、参数记成空——整轮历史
    不能因为一个坏参数就错位，那会让后面每一轮都答非所问。
    """
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if not isinstance(fn, dict):
            fn = item
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        out.append({"name": str(name), "arguments": args})
    return out


def render_assistant_turn(content, tool_calls):
    """助手轮里「正文 + 若干 tool_call」那一段（不含 `<|im_start|>assistant\\n` 与
    `<|im_end|>`）。

    KV 记账和 prompt 渲染**共用这一个函数**，两边的字节因此天然一致——下一轮 prompt
    必须以「本轮的 prompt + 本轮回复」开头，否则粘性复用退化成每轮全量重算，而那种
    退化不报错、答案也是对的，只能靠这个不变量守住。

    参数值优先用 `raw`（解析时留下的**原文**）：模型写 `true` 我们解析成 Python 的
    `True`，再 `str()` 回去就成了 `True`——`1.50` -> `1.5`、`null` -> `None` 同理。
    那点差异本身不会答错（KV 里装的是模型的真实输出，而我们只往后追加，追加点上两个
    世界是重合的），但它会让"记账文本 == KV 里实际装的东西"这条不变量从"逐字节为真"
    退化成"大致成立"，而这条不变量是这个项目唯一的安全网。客户端回显的历史没有 `raw`
    （arguments 本来就是 JSON 值），退回 `_param_text`。

    **正文的 rstrip 在这一处做**（有调用时）。不放在拆分侧是因为流式路径没法"撤回"已经
    发给客户端的字节：拆分侧去 rstrip 的话，流式客户端拿到的正文结尾仍带着模型用来分隔
    的那几个空白，而非流式拿到的是去掉的——两条路径返回的正文不一致。客户端把拿到的
    正文原样回显（agent 框架基本都这么干），渲染出来的字节就和我们记账的对不上，粘性
    复用静默退化成每轮全量重算。放在这里则两条路径返回同样的正文，规范化只在"定义规范
    字节形态"的这一处发生。
    """
    body = content or ""
    if tool_calls:
        body = body.rstrip()
    out = [body]
    for i, call in enumerate(tool_calls):
        if i == 0:
            # 模板：正文非空时用两个换行把它和第一个调用隔开；正文为空则不留空行。
            if body.strip():
                out.append("\n\n")
        else:
            out.append("\n")
        out.append("<tool_call>\n<function=%s>\n" % call["name"])
        raw = call.get("raw") or {}
        for key, value in (call.get("arguments") or {}).items():
            out.append("<parameter=%s>\n%s\n</parameter>\n"
                       % (key, raw[key] if key in raw else _param_text(value)))
        out.append("</function>\n</tool_call>")
    return "".join(out)


def to_openai_tool_calls(calls, prefix="call"):
    """内部形态 -> OpenAI 的 `tool_calls` 数组（`arguments` 是 JSON 字符串）。

    `id` 只要在**这一条消息内**唯一即可：客户端回显它，但我们渲染 prompt 时用的是
    name + arguments，id 不进 prompt。所以用下标构造就够了，不需要随机数
    （没有随机数 = 同一输入永远同一输出，便于对拍）。
    """
    out = []
    for i, call in enumerate(calls):
        out.append({
            "id": "%s_%d" % (prefix, i),
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
            },
        })
    return out


# --- 解析 -----------------------------------------------------------------

_FUNCTION_RE = re.compile(r"<function=([^>\n]+)>")
# 值可以跨行（模板明确允许），所以用 DOTALL + 非贪婪；`\n</parameter>` 保证不会
# 把一个多行值截断——只有真正的闭合标签才匹配得上。
_PARAM_RE = re.compile(r"<parameter=([^>\n]+)>\n(.*?)\n</parameter>", re.S)


def _param_value(text):
    """参数值 -> Python 对象。能当 JSON 解就当 JSON（数字/布尔/容器），否则留字符串。

    给客户端的 `tool_calls.arguments` 要是**有类型**的 JSON：`"5"` 和 `5` 对用 JSON
    Schema 校验的工具来说不是一回事，参数保持字符串会让一部分客户端直接报参数错误。

    代价是这一步**有损**：`true` -> `True`、`1.50` -> `1.5`、`null` -> `None`。所以
    解析时把原文一并留在 `raw` 里，渲回 prompt 时用原文（见 render_assistant_turn）。
    """
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_call_body(body):
    """`<tool_call>` 和 `</tool_call>` 之间的内容 ->
    {"name":..., "arguments": {...}, "raw": {参数名: 原文}}；
    认不出来返回 None（调用方会把原文当正文交回去，不吞内容）。

    `raw` 只服务于"渲回 prompt 时逐字节还原"（见 render_assistant_turn）：arguments
    给客户端用（有类型），raw 给 KV 记账用（原样）。两个都在，谁都不用将就。
    """
    m = _FUNCTION_RE.search(body)
    if m:
        args, raw = {}, {}
        for key, value in _PARAM_RE.findall(body):
            args[key] = _param_value(value)
            raw[key] = value
        return {"name": m.group(1).strip(), "arguments": args, "raw": raw}

    # 兜底：Qwen3 早期的 Hermes JSON 形态。模型偶尔会回退，认一下成本很低，
    # 不认的话这一轮的工具调用就静默变成一段没人执行的正文。
    try:
        obj = json.loads(body.strip())
    except ValueError:
        return None
    if not isinstance(obj, dict) or not obj.get("name"):
        return None
    args = obj.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    return {"name": str(obj["name"]),
            "arguments": args if isinstance(args, dict) else {}}


class ToolCallExtractor(object):
    """从模型回复里摘出 `<tool_call>` 块，其余原样透传。**流式与非流式共用这一个实现**。

    为什么不能逐帧正则：DELTA 是按 token 投递的，`<tool_call>` 和 `</tool_call>` 都可能
    被切在两帧之间（板卡桩后端甚至故意按 7 字节切）。所以维持一个小状态机，只在"还可能
    是在形成一个标签"的极短窗口里攒字节，一旦判定不是就立刻透传，不等。

    为什么要认 `<think>`：开思考时模型是「先推理、后调用」，但一个只是在**讨论**
    "这里应该调用 xxx" 的推理过程里也可能出现 `<tool_call>` 字样。那种情况下把它抽出来
    当成真调用，等于替模型执行了它没打算执行的工具。所以推理段内的标签一律当正文。
    （关思考时不需要：那条路上 ThinkStripper 已经把开头的 think 段摘掉了。）
    """

    MAX_HOLD = 1 << 16      # 攒这么多还没闭合就放弃（截断的回复，见下面 body 分支）

    def __init__(self, reasoning=False):
        self.reasoning = bool(reasoning)
        self.buf = ""
        self.in_call = False
        self.in_reason = False
        self.calls = []

    # -- 内部：当前该盯哪些标签 --

    def _marks(self):
        if self.in_reason:
            return ("</think>",)
        if self.reasoning:
            return (MARK_OPEN, "<think>")
        return (MARK_OPEN,)

    def _next_mark(self):
        best = None
        for mark in self._marks():
            i = self.buf.find(mark)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, mark)
        return best if best else (-1, None)

    def _hold_len(self):
        """结尾有多少字节可能是某个标签的开头——这些必须留着等下一帧。"""
        marks = self._marks()
        limit = min(len(self.buf), max(len(m) for m in marks) - 1)
        for k in range(limit, 0, -1):
            tail = self.buf[-k:]
            if any(m.startswith(tail) for m in marks):
                return k
        return 0

    # -- 对外 --

    def feed(self, text):
        """送进一段（可能是半个字的）新文本，返回 (可以发出的正文, 这次新完成的调用)。"""
        if text:
            self.buf += text
        out = []
        done = []
        while self.buf:
            if self.in_call:
                j = self.buf.find(MARK_CLOSE)
                if j < 0:
                    if len(self.buf) > self.MAX_HOLD:
                        # 吐了个不闭合的 <tool_call>（通常是 max_tokens 截断）。
                        # 连标签一起当正文交回去：宁可让用户看见半个调用，也不能吞掉。
                        out.append(MARK_OPEN + self.buf)
                        self.buf = ""
                        self.in_call = False
                    break
                body = self.buf[:j]
                self.buf = self.buf[j + len(MARK_CLOSE):]
                self.in_call = False
                call = parse_call_body(body)
                if call is None:
                    # 认不出来就原样交回正文。**不猜**：编一个参数错误的调用比不调用更糟。
                    out.append(MARK_OPEN + body + MARK_CLOSE)
                else:
                    self.calls.append(call)
                    done.append(call)
                continue

            i, mark = self._next_mark()
            if mark is not None:
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(mark):]
                if mark == MARK_OPEN:
                    self.in_call = True
                else:
                    # <think>/</think> 是**正文的一部分**（本项目开思考时把推理原样透传，
                    # 见 rkllm_gateway.ThinkStripper 的说明），标签必须跟着发出去——它
                    # 在这里只用来开关"当前这段是不是推理"。顺手丢掉的话，记账文本会比
                    # 模型当初生成的少两个标签，下一轮 prompt 的前缀就对不上。
                    out.append(mark)
                    self.in_reason = (mark == "<think>")
                continue

            hold = self._hold_len()
            if hold:
                out.append(self.buf[:-hold])
                self.buf = self.buf[-hold:]
            else:
                out.append(self.buf)
                self.buf = ""
            break
        return "".join(out), done

    def flush(self):
        """收尾：把还攒着的字节交出来（截断在半个标签里时要连标签一起还，别吞正文）。"""
        out, self.buf = self.buf, ""
        if self.in_call:
            out = MARK_OPEN + out
            self.in_call = False
        return out, []


def split_tool_calls(text, reasoning=False):
    """一次性把回复拆成 (正文, 调用列表)。非流式路径用它，流式路径用 ToolCallExtractor。

    正文是模型原输出的**忠实前缀**，末尾空白照留——规范化（有调用时 rstrip）在
    render_assistant_turn 里做，这样它和流式路径返回的正文逐字节相同，见那里的说明。
    """
    ex = ToolCallExtractor(reasoning=reasoning)
    content, calls = ex.feed(text)
    tail, _ = ex.flush()
    return content + tail, calls


# --- 自检 -----------------------------------------------------------------

CALL_XML = ("<tool_call>\n<function=get_weather>\n<parameter=city>\n北京\n</parameter>\n"
            "</function>\n</tool_call>")


def _drip(raw, n, reasoning=False):
    """按 n 个字符一段投递（模拟 DELTA 被切开），返回 (正文, 调用列表)。"""
    ex = ToolCallExtractor(reasoning=reasoning)
    content, calls = [], []
    for i in range(0, len(raw), n):
        t, done = ex.feed(raw[i:i + n])
        content.append(t)
        calls += done
    t, _ = ex.flush()
    content.append(t)
    return "".join(content), calls


def selftest(check):
    """纯字符串自检，不需要后端。由网管的 --selftest 和 check_template.py 共同调用。

    `check(name, cond, detail)` 由调用方给，两个入口的签名一致。
    """
    print("== 工具调用解析：一次性 / 逐字符 / 逐 7 字节（DELTA 会把标签切成两帧）==")
    # 这三种粒度必须**逐字节等价**，而且顺带钉住"流式与非流式返回同样的正文"：正文是
    # 模型原输出的忠实前缀（末尾空白照留），规范化只在渲染侧做一次。两边不一致的话，
    # 客户端回显流式拿到的正文再发下一轮，渲染出来的字节就和记账的对不上，粘性复用
    # 静默退化成每轮全量重算——不报错、答案也对，只能靠这条断言守住。
    raw = "北京是晴的。\n\n" + CALL_XML
    want_calls = [{"name": "get_weather", "arguments": {"city": "北京"},
                   "raw": {"city": "北京"}}]
    for n in (len(raw), 1, 7):
        got = _drip(raw, n)
        # 逐字符那一组是关键：板卡桩后端就按 7 字节切帧，`<tool_call>` 完全可能断在
        # "<tool_ca" + "ll>"。逐帧正则一定漏，所以这里必须三种投递等价。
        check("投递粒度 %d: %r" % (n, "整段" if n == len(raw) else "%d 字符" % n),
              got == ("北京是晴的。\n\n", want_calls),
              "正文=%r 调用=%r" % (got[0], got[1]))

    print("== 开思考：推理段里的 <tool_call> 只是「提到」，不是要调用 ==")
    # 抽出来执行它，等于替模型执行了它没打算执行的工具。推理段的标签一律当正文。
    think_raw = "<think>我应该 <tool_call> 一下</think>那我查一下。\n\n" + CALL_XML
    c, calls = split_tool_calls(think_raw, reasoning=True)
    check("推理段内的 <tool_call> 不算调用",
          c.rstrip() == "<think>我应该 <tool_call> 一下</think>那我查一下。"
          and calls == want_calls,
          "正文=%r 调用数=%d" % (c, len(calls)))
    check("<think>/</think> 标签本身留在正文里（记账要逐字节还原）",
          c.startswith("<think>") and c.rstrip().endswith("</think>那我查一下。"), "正文=%r" % c)

    print("== 关思考：<think> 段由 ThinkStripper 摘，这里只保证标签不被当标记吞掉 ==")
    c, calls = split_tool_calls("<think>  </think>  北京是晴的。\n\n" + CALL_XML, reasoning=False)
    check("关思考路径：正文含 <think> 段、调用摘出",
          c.rstrip() == "<think>  </think>  北京是晴的。" and calls == want_calls,
          "正文=%r 调用数=%d" % (c, len(calls)))

    print("== 认不出来的块原样交回正文（不猜、不吞）==")
    unknown = "<tool_call>\n随便写点什么\n</tool_call>剩下的"
    c, calls = split_tool_calls(unknown)
    check("块里没有 <function=...>：原文交回", calls == [] and c == unknown,
          "正文=%r 调用数=%d" % (c, len(calls)))
    # 无参数的调用是**合法**的（模板里 noop 那种），不能因为它没 <parameter> 就当认不出来
    c, calls = split_tool_calls("<tool_call>\n<function=f>\n</tool_call>尾巴")
    check("无参数的调用仍然认出来", calls == [{"name": "f", "arguments": {}, "raw": {}}]
          and c == "尾巴", "正文=%r 调用=%r" % (c, calls))

    print("== 截断（没闭合）连标签一起交回，不吞正文 ==")
    c, calls = split_tool_calls("正文<tool_call>\n<function=f>")
    check("半截调用作为正文返回", calls == [] and c == "正文<tool_call>\n<function=f>",
          "正文=%r 调用数=%d" % (c, len(calls)))
    c, calls = split_tool_calls("abc<to")
    check("正文里出现半个标签开头时不吞不丢", calls == [] and c == "abc<to", "正文=%r" % c)

    print("== 往返：拆分后再渲回 == 模型原始输出（KV 前缀的地基）==")
    # 这一条是整个粘性复用的地基：记账文本一旦和模型当初生成的字节不一致，下一轮就是
    # 把差异段接在一段对不上的历史上——模型会以一个自信的错答案正常返回，不报错、不留痕。
    raw_full = "<think>\n推理\n</think>\n\n北京是晴的。\n\n" + CALL_XML
    c, calls = split_tool_calls(raw_full, reasoning=True)
    check("开思考：拆分 -> 渲染 逐字节还原",
          render_assistant_turn(c, calls) == raw_full,
          "\n     还原=%r\n     原始=%r" % (render_assistant_turn(c, calls), raw_full))

    nt = "北京是晴的。\n\n" + CALL_XML
    c2, calls2 = split_tool_calls(nt, reasoning=False)
    check("关思考：拆分 -> 渲染 逐字节还原",
          render_assistant_turn(c2, calls2) == nt,
          "\n     还原=%r" % (render_assistant_turn(c2, calls2),))

    # 参数值的**原文**必须原样回去：`true` 解析成 Python 的 True，`str()` 回去是 `True`；
    # `1.50` -> `1.5`、`null` -> `None` 同理。差这几个字节本身答不错（KV 里是模型的真实
    # 输出，我们只在末尾追加，追加点上两个世界重合），但"记账 == KV 里装的东西"这条
    # 不变量会从"逐字节为真"退化成"大致成立"，而它是这个项目唯一的安全网。
    for v in ("true", "false", "null", "1.50", "007", "-0", "1e3", '{"b":2,"a":1}', "多行\n值"):
        one = ("<tool_call>\n<function=f>\n<parameter=p>\n%s\n</parameter>\n</function>\n"
               "</tool_call>" % v)
        cc, cl = split_tool_calls(one)
        check("参数原文往返: %r" % v, render_assistant_turn(cc, cl) == one,
              "\n     还原=%r\n     原始=%r" % (render_assistant_turn(cc, cl), one))

    print("== 给客户端的那一份是**有类型**的 JSON（原文不外泄）==")
    _, cl = split_tool_calls("<tool_call>\n<function=f>\n<parameter=p>\ntrue\n</parameter>\n"
                             "</function>\n</tool_call>")
    oai = to_openai_tool_calls(cl)
    check("arguments 是 JSON 字符串且真值是 true（不是 \"true\"）",
          oai[0]["function"]["arguments"] == '{"p": true}',
          "= %r" % oai[0]["function"]["arguments"])
    check("内部字段 raw 不外泄", "raw" not in oai[0]["function"] and "raw" not in oai[0],
          "= %r" % (oai[0],))

    print("== 没有工具调用时，正文一个字节都不变（老行为）==")
    for plain in ("收到", "<think>  </think>  收到", "北京是晴的。", ""):
        c, calls = split_tool_calls(plain, reasoning=False)
        check("无调用: %r" % plain, calls == [] and c == plain, "正文=%r" % c)

    print("== 兜底认 Hermes JSON（不带 raw，往返会对不上——已知限制）==")
    # 渲染侧一律用 XML，所以这条路渲染回去必然不是原文 => 前缀对不上 => 那之后每轮多花
    # 一次全量 prefill。认它是因为不认的话这一轮调用会静默变成一段没人执行的正文，比多
    # 花一次 prefill 糟得多。只影响模型自己回退到老格式的那些轮。
    hermes = '<tool_call>\n{"name": "get_weather", "arguments": {"city": "北京"}}\n</tool_call>'
    c, calls = split_tool_calls(hermes)
    check("Hermes JSON 也能认出来",
          calls == [{"name": "get_weather", "arguments": {"city": "北京"}}],
          "调用=%r" % (calls,))
    check("（已知限制）Hermes 路径渲染回去与原文不同",
          render_assistant_turn(c, calls) != hermes,
          "渲染=%r" % (render_assistant_turn(c, calls),))

    print("== 不闭合的调用不会把内存吃光 ==")
    huge = "<tool_call>" + "x" * (ToolCallExtractor.MAX_HOLD + 1)
    ex = ToolCallExtractor()
    out, calls = ex.feed(huge)
    tail, _ = ex.flush()
    out += tail
    check("超过 MAX_HOLD 就放弃、连标签交回正文",
          calls == [] and out == huge, "长度=%d 期望=%d" % (len(out), len(huge)))

