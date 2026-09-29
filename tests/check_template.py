#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板逐字节核对 + 粘性复用前提核对。

为什么要单独核这个：网关自己渲染 prompt（demo 内置模板只有「首轮/后续轮」两态，
表达不了 Agent 发来的任意角色历史），而渲染结果**差一个字符不会有任何报错**，
只会让模型收到的对话格式悄悄变味、输出质量下降。这类错误只能靠逐字节比对抓。

第二项（前缀性）是网关能不能只发差异部分的**前提**：第二轮拼出来的完整 prompt
必须以「上一轮的 prompt + 上一轮回复」开头，否则粘性复用会把模型带到一个
自相矛盾的上下文里。

第三项是工具调用（function calling）的渲染。**main.cc 里的 QWEN35_CHAT_TEMPLATE 没有
工具支持**，所以拿它当权威是错的——权威是模型自己的 `tokenizer.chat_template`。板卡上
没有 jinja2（实测 Python 3.12 无此包），渲不了，于是参考字节在本机用真 Jinja2 渲好后冻
进 `tool_golden.json`（生成器 rt_work/gen_golden.py，不进 git）。比对的仍然是模板的真
输出；只是把「渲染」这一步挪到了生成那一刻。
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
from rkllm_gateway import render_messages                        # noqa: E402
from toolcalls import selftest as toolcalls_selftest             # noqa: E402

# 与 src/main.cc 的 QWEN35_CHAT_TEMPLATE 完全一致
SYS = ("<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. "
       "You are a helpful assistant.<|im_end|>\n")
UPRE = "<|im_start|>user\n"
UPOST = "<|im_end|>\n<|im_start|>assistant\n"

# 这里**故意**写死、不从 rkllm_gateway import：本文件的全部价值就是当一份独立的记录，
# 常量都从被测代码拿的话，"两边同时改错"就永远抓不到了。
ASSISTANT_HEAD = "<|im_start|>assistant\n"
THINK_OFF = "<think>\n\n</think>\n\n"

fails = []


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        fails.append(name)


print("== 与 main.cc 的模板逐字节比对 ==")
mine1 = render_messages([{"role": "user", "content": "你好"}])
want1 = SYS + UPRE + "你好" + UPOST
check("首轮（system + user）", mine1 == want1)
if mine1 != want1:
    print("     mine: %r" % mine1)
    print("     want: %r" % want1)

# 注意 UPOST 本身就以 "<|im_start|>assistant\n" 结尾（它就是 assistant 头），
# 所以 assistant 那一段只需接内容 + <|im_end|>，不要再补一个 assistant 头。
mine2 = render_messages([{"role": "user", "content": "A"},
                         {"role": "assistant", "content": "B"},
                         {"role": "user", "content": "C"}])
want2 = (SYS + UPRE + "A" + UPOST
         + "B" + "<|im_end|>\n"
         + UPRE + "C" + UPOST)
check("多轮（user/assistant/user）", mine2 == want2)
if mine2 != want2:
    print("     mine: %r" % mine2)
    print("     want: %r" % want2)

print("== 粘性复用的前提 ==")
prev_full = mine1
gen = "上一轮的回答"
follow = render_messages([{"role": "user", "content": "你好"},
                          {"role": "assistant", "content": gen},
                          {"role": "user", "content": "继续"}])
check("第二轮 prompt 以 上一轮prompt+回复 为前缀", follow.startswith(prev_full + gen),
      "suffix=%d 字 / full=%d 字" % (len(follow) - len(prev_full + gen), len(follow)))
check("差异部分非空", len(follow) > len(prev_full + gen))

print("== /no_think 贴在所有 user 上（跨轮渲染才稳定）==")
# 这条曾经写成"只贴最后一条 user"，并且测试也跟着这么断言——那是错的：第一轮 u1 是
# "最后一条"（带标记），第二轮 u1 变成历史消息（不带）=> 同一段历史两轮渲染的字节不同
# => 前缀判据失败 => 关思考时每轮全量重算（实测：第二轮 reset=1, base=0, sent=full）。
# 所以这里断言的不是"贴哪一条"，而是**同一个历史在两轮里渲染成同样的字节**。
m3 = render_messages([{"role": "user", "content": "A"}, {"role": "user", "content": "C"}],
                     enable_thinking=False)
check("每条 user 都带 /no_think", "A /no_think<|im_end|>" in m3 and "C /no_think<|im_end|>" in m3)
check("不传 enable_thinking 时不贴 /no_think",
      "/no_think" not in render_messages([{"role": "user", "content": "A"}])
      and "/no_think" not in render_messages([{"role": "user", "content": "A"}],
                                             enable_thinking=True))

print("== 关思考下第二轮 prompt 仍以第一轮为前缀（粘性复用前提）==")
u1 = {"role": "user", "content": "记住暗号：青柠味苏打水。"}
h1 = render_messages([u1], enable_thinking=False)
h2 = render_messages([u1, {"role": "assistant", "content": "收到"},
                      {"role": "user", "content": "暗号是什么？"}], enable_thinking=False)
check("关思考：第二轮以第一轮为前缀", h2.startswith(h1),
      "suffix=%d 字 / full=%d 字" % (len(h2) - len(h1), len(h2)))
check("关思考：差异部分非空", len(h2) > len(h1))

# 上面那条**不是**网关真正用的判据。网关记的 `known` 是「本轮发出去的 prompt + 模型回复」，
# 判的是 `新 prompt.startswith(known)`。补上 think 尾巴之后这条必须重新验一遍：
# 尾巴只加在生成位置，而 `h1 + 回复` 里那个位置**也**带着同一个尾巴，两边字节要对得上，
# 否则第二轮起 reset 永远为 1、每轮全量重算——只慢不错，屏幕上一点异常都看不见。
check("关思考：第二轮以「第一轮 + 回复」为前缀（= 网关真正用的 KV 复用判据）",
      h2.startswith(h1 + "收到"), "suffix=%d 字" % (len(h2) - len(h1) - len("收到")))
check("关思考：生成位置补了模板那对空标记", h1.endswith(ASSISTANT_HEAD + THINK_OFF),
      "尾 24 字 = %r" % h1[-24:])

print("== 工具调用渲染：与模型自己的 chat_template 逐字节比对（金标准）==")
# 逐字节比对抓得到而人眼抓不到的：工具定义走 Jinja 的 tojson（**键排序**、中文转成
# \uXXXX、`<` `>` `&` `'` 全转义），助手轮第一条调用前的分隔是 \n\n 还是 \n，连续的
# tool 消息是合成一个 user 轮还是各成一轮——这些差一点都不会报错，只会让模型收到的
# 对话格式悄悄变味。三处曾经真的写错过，都是这一项抓出来的。
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_golden.json")
with io.open(GOLDEN, encoding="utf-8") as fp:
    golden = json.load(fp)

WRAP = golden["wrap"]           # 模板给助手轮加的推理包装
WRAP_OFF = golden["wrap_off"]
for case in golden["cases"]:
    want = case["want"]
    if not want.endswith(case["gen_tail"]):
        check(case["name"], False, "金标准不以 gen_tail 结尾（模板变了，重跑 gen_golden.py）")
        continue

    if case["enable_thinking"] is False:
        # 关思考：与模板**逐字节直比**，不做任何替换。两处"刻意偏差"在 2026-09-18
        # 一起消掉了——` /no_think` 那个软开关挡不住"要组织语言"的题（板上实测
        # `介绍一下杭州` 会写满一整段 `<think>Thinking Process:…`，256 token 一截
        # 就是屏幕上那堵推理墙），补回模板的生成尾巴之后，模型从 `</think>` 后面开始
        # 生成，结构上开不了 think 段。
        #
        # 只有一处金标准要动，而且这处偏差是**我们比模板更贴近真实 token 流**：
        # 模板的关思考分支把历史里的助手轮渲成光秃秃的 `<|im_start|>assistant\n`
        # （它的假设是"关思考 ⇒ 助手轮里没有 think 段"），而生成位置补上尾巴之后，
        # KV 里每轮**确实**都留着那对空标记；历史不补的话，第二轮 prompt 的前缀就对不上
        # KV 里真正装着的东西，粘性复用会静默全废（`nothink_check.py` 盯的就是这个）。
        body, tail = want[:-len(case["gen_tail"])], want[-len(case["gen_tail"]):]
        n_hist = sum(1 for m in case["messages"] if (m.get("role") or "") == "assistant")
        # 先数一遍再替换：替换 0 个还报 PASS 就是空转，那条用例根本没在测东西。
        if body.count(ASSISTANT_HEAD) != n_hist or n_hist == 0:
            check(case["name"], False,
                  "金标准的助手轮数 %d != 消息里的 %d（模板变了，重跑 gen_golden.py）"
                  % (body.count(ASSISTANT_HEAD), n_hist))
            continue
        want = body.replace(ASSISTANT_HEAD, ASSISTANT_HEAD + THINK_OFF) + tail
    else:
        # 思考开着：两处偏差照旧（理由见 toolcalls.py 顶部）——助手轮的推理包装里要放
        # 模型当时的推理，而它在渲染侧拿不到；生成尾巴归模型自己吐。这里把那截尾巴
        # **显式换掉**，偏差留在比对代码里。
        want = want[:-len(case["gen_tail"])] + ASSISTANT_HEAD
        n_wrap = want.count(WRAP)
        want = want.replace(WRAP, WRAP_OFF)
        # 替换 0 个还报 PASS 就是空转（那条用例根本没在测推理包装）。关思考那支的
        # 对应守卫在上面（数助手轮数），两支都不能少——这个套件最容易退化成"全绿但
        # 什么都没测"，而它盯的恰恰是"差一个字节不报错、只让模型收到的格式悄悄变味"。
        if n_wrap != case["wrappers"]:
            check(case["name"], False,
                  "推理包装数 %d != 记录 %d（模板变了，重跑 gen_golden.py）"
                  % (n_wrap, case["wrappers"]))
            continue

    got = render_messages(case["messages"], enable_thinking=case["enable_thinking"],
                          tools=case["tools"] or None)
    # /no_think 由 render_messages 自己贴，金标准里的 messages 是**没贴过**的原文，
    # 所以这里不预处理；no_think_marks 只用来核对"参考渲时确实贴了"。
    marks_ok = (" /no_think" in want) if case["no_think_marks"] else ("/no_think" not in want)
    ok = (got == want) and marks_ok
    detail = ""
    if not ok:
        if not marks_ok:
            detail = "/no_think 标记与记录不符"
        else:
            i = next((k for k, (a, b) in enumerate(zip(want, got)) if a != b), None)
            detail = ("第 %d 字节起不同\n     want=%r\n     got =%r" % (i, want[i:i + 40], got[i:i + 40])
                      if i is not None else "\n     want=%r\n     got =%r" % (want, got))
    check(case["name"], ok, detail)

print("")
# 回复侧的解析（含"拆分->渲染 == 模型原始输出"这条 KV 前缀的地基）。跟渲染一样是纯字符
# 串逻辑，不需要后端，板卡上也能跑——所以它必须进这个套件，不能只留在本机的临时脚本里。
toolcalls_selftest(check)

print("\n%s" % ("模板检查通过" if not fails else "失败项：%s" % ", ".join(fails)))
sys.exit(0 if not fails else 1)
