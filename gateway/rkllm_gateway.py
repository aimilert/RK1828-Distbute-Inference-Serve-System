#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rkllm_multicard_demo（--serve）前面的 OpenAI 兼容网关。

为什么要有这一层：官方 rkllm3-server 的 OpenAI 接口是现成的，但文档 §4.5.2 写明
「推理执行排队串行」，实测 --n-session 1/2/4 的聚合都是 ~11 tok/s（1.03x）——
4 个 slot 只提供隔离、不提供并行。而我们自己的 demo 在同一硬件上是 3.33x
（10.73 -> 35.74 tok/s，见方案文档 §9.2）。所以 HTTP 这一层自己写，推理仍然走
我们自己验证过的多会话执行器。

架构（三个进程角色，全在板卡上）：
    workflow agent ──HTTP/OpenAI──▶ rkllm_gateway.py ──帧协议(stdin/fd)──▶ rkllm_multicard_demo --serve
                          (本文件)                         (一个进程, N 个会话)

网关做三件事：
  1. 把 OpenAI 的 messages 按 Qwen3.5 模板渲染成一个 prompt 字符串（渲染在网关侧做，
     因为 demo 内置的模板只有「首轮 / 后续轮」两态，表达不了任意角色的历史消息）；
  2. 会话粘性：把「同一段对话」钉在同一个 demo 会话上，并只在会话已有的 KV 之后
     补发差异部分，让多轮对话不必每轮重算整个上下文（长上下文下这是每轮上百秒的差别）；
  3. 把帧协议翻成 OpenAI 的 JSON / SSE。并发请求落到不同会话上，由 demo 的执行器
     跨阶段重叠，这才是 3.33x 的来源。

安全性上有一条不能省：**前缀校验**。粘性记录只是"猜测"，真正决定能不能只发差异部分的
是「本次完整 prompt 是否以该会话已知文本为前缀」。不满足就 RESET + 发全量。所以粘性记录
错了最多是慢，不会答出串味的结果。

用法（板卡上）：
    ./start_gateway.sh                       # 见同目录的启动脚本
    python3 rkllm_gateway.py --port 18280 --sessions 4 -- <demo argv...>
    python3 rkllm_gateway.py --selftest -- <demo argv...>   # 只验帧协议，不起 HTTP
"""
import argparse
import codecs
import collections
import glob
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 端侧 MLFQ 调度内核：纯逻辑、只用标准库。默认**不启用**（SCHED_POLICY=fcfs），
# 只有显式打开才会改变"会话空出来时派给谁"。见 mlfq_sched.py 的模块注释。
import mlfq_sched

# 工具调用的渲染/解析单独一个文件：格式细节多（XML 形态、tojson 的坑、连续 tool 消息
# 合并成一个 user 轮），而且它自己就能被逐字节对拍（check_template.py），不该混在
# 已经 1800 行的网关里。
from toolcalls import (ToolCallExtractor, normalize_tool_calls, render_assistant_turn,
                       render_tools_system, split_tool_calls, to_openai_tool_calls,
                       tool_response_block)
from toolcalls import selftest as toolcalls_selftest

MODEL_ID = os.environ.get("MODEL_ID", "qwen3.5-27b")   # 展示用（/health、/v1/models）；可用 MODEL_ID 环境变量覆盖

# ---- 请求转储（调试用，默认关）----
# DUMP_REQ=1 时把每次 /v1/chat/completions 的**摘要**追加到 jsonl，用来回答"客户端到底发了什么"
# （例如 Claude Code 并发发的那个无工具小请求是什么、本轮调了哪些工具）。
# 只写摘要：每条消息的 role/长度/前 DUMP_REQ_CHARS 个字符 + 工具名清单；不写全量正文。
DUMP_REQ = os.environ.get("DUMP_REQ", "") not in ("", "0", "false", "False", "no")
DUMP_REQ_FILE = os.environ.get("DUMP_REQ_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "request_dump.jsonl")
try:
    DUMP_REQ_CHARS = int(os.environ.get("DUMP_REQ_CHARS") or 400)
except ValueError:
    DUMP_REQ_CHARS = 400
BACKEND_READY_TIMEOUT = 900.0     # 模型加载约 230-240s，留足余量
REQUEST_TIMEOUT = 1800.0          # 单轮最长等待（长上下文 prefill 很慢）
DEFAULT_MAX_NEW_TOKENS = 512

# 会话不够时怎么办。板卡上会话的硬上限实测是 5 个（第 6 个 session_init 在 stage0 失败），
# 而且只能在启动时建，运行期建不了。所以**同时活跃的对话数**就是并发上限，第 N+1 段对话
# 只能等——等谁腾出来。
#
# 谁腾？一段对话静默超过 IDLE_TTL 就把它的会话收回。这两个默认值是**配对**的：
# 队列最长也就等到"最闲的那段对话静默满 TTL"，所以 QUEUE_TIMEOUT 要留得比 IDLE_TTL 大，
# 否则会出现"明明马上就轮到了，请求却先超时报 503"。
#
# IDLE_TTL 别调太小：10 个人活跃、只有 5 个会话时，每个人都在 TTL 后被踢掉、又排队等回来，
# 每一轮都要全量重算 prefill（多花 2~3 秒）。它必须明显大于"正常人相邻两轮之间的间隔"。
DEFAULT_IDLE_TTL = 300.0          # 秒；0 = 不回收（= 第 N+1 段对话永远排队）
DEFAULT_QUEUE_TIMEOUT = 600.0     # 秒；排队超过它就返回 503，不要让终端用户干等
# 上面那对默认值有个不对称，2026-09-18 被用户抓出来："NPU 明明没人用，输入问题后还是
# 在等"。**只有 IDLE_TTL 一条路决定谁腾会话**，而它被调得很大（300s）是为了保住 KV
# 复用——可这段静默期里 NPU 是**真闲着**，等的人却在空转。板上实测（`rt_work/
# page_repro.py` 的 queue 场景）：四段对话各自静默 70~90s，第 5 段对话干等 **226.0s**
# 才拿到会话，全程四个会话都是 idle。等待时长 = IDLE_TTL − 它自己静默了多久。
#
# 所以把"静默"分成两级：
#   · 没人等：按 IDLE_TTL（300s）——保住"多轮对话的第二轮直接命中 KV"这条主路径；
#   · 有人在等（请求进来发现一个空闲会话都没有，非等不可）：按 CONTEND_IDLE 收，
#     取**最闲的那一段**对话，只收一个。多花的是被收走那段对话下一轮的 prefill
#     （页面上这种两百来字节的对话不到 0.3s），换来的是让等的人从"分钟级"降到"秒级"。
# 这不违反"不抢占"：只收**没在跑**的会话，而且按静默时长挑受害者（不是按请求到达顺序），
# 行为可预期。CONTEND_IDLE 必须明显大于"同一段对话相邻两轮的真实间隔"，否则一个正常的
# 多轮用户每轮都在被收走、每轮都全量重算。
DEFAULT_CONTEND_IDLE = 15.0       # 秒；0 = 关掉这一级（退回"永远等到 IDLE_TTL"）

# ---- 改动 A：窗口裁剪（单会话在有界内存内滚动）--------------------------------
# 客户端（Claude Code）每一轮都把**全量** messages 发上来，所以网关手里天然有完整
# 历史：一旦渲染后的 prompt 超过"上下文预算"，就**保 system + 最近若干轮**、丢掉最老的
# 非 system 消息，再以 reset 全量重发。这样单会话可以一直聊下去，不会再因为"history
# 攒太长"被后端 REJECT（HTTP 400，Claude 侧表现为"上下文满了发不出去"）。
#
# 预算 = 后端 ctx_size − CTX_RESERVE − 本轮输出预留。ctx_size 取模型内建上限（与后端
# backend.ctx_size 同源）；精确 token 数走后端新增的 COUNT 帧（网关没有 tokenizer）。
#
# 注意这是**有损**的：被丢掉的老历史不再参与注意。保 system 是为了不丢"角色/工具"这类
# 长期约束；近期若干轮保的是当前任务上下文。默认开启，可用 --no-window 关掉退回旧行为。
DEFAULT_CTX_RESERVE = 512          # 给 prefill/解码尾巴留的余量（token）
DEFAULT_WINDOW_MIN_MESSAGES = 2    # 至少保留的非 system 消息数（再挤也得留住最近一问一答）
DEFAULT_WINDOW_MAX_OUTPUT = 4096   # 计算输出预留时对 max_tokens 的封顶（防客户端写天文数字）
HISTORY_OMIT_MARKER = "\n[earlier history omitted: %d message(s) dropped to fit the context]\n"
COUNT_TIMEOUT = 60.0               # 等后端 COUNTED 帧的上限（计数是微秒级，只有后端忙才慢）
# 送进后端 tokenizer 的计数载荷上限。超过就不送了——预编译 tokenizer 在超长输入上的
# 行为没有保证（实测一次 ~150KB 的计数载荷把后端进程整个带走过），改为按 3 字节/token
# 保守估算。下游只用它判"超没超预算"，估算足够；真正精确的计数留在裁剪后的最后一步。
MAX_COUNT_BYTES = 96 * 1024
KV_OP_TIMEOUT = 600.0              # 等后端 SAVED/LOADED 帧的上限（GB 级 KV 走 PCIe 要时间）
DEFAULT_KV_SWAP_CAP = 16           # 换页文件最多留几段对话（LRU 淘汰）
DEFAULT_KV_SWAP_MAX_MB = 4096      # 换页文件总字节配额（MB）；到量按 LRU 删最老的
KV_SWAP_MIN_FREE_BYTES = 512 * 1024 * 1024  # 落盘前要求目标盘至少这么大余量，否则跳过换页

IM_START = "<|im_start|>"
IM_END = "<|im_end|>\n"
# 与 src/main.cc 的 QWEN35_CHAT_TEMPLATE 保持一致：
#   system_prompt = "<|im_start|>system\n...<|im_end|>\n"
#   user_prefix   = "<|im_start|>user\n"
#   user_postfix  = "<|im_end|>\n<|im_start|>assistant\n"
QWEN35_DEFAULT_SYSTEM = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

ASSISTANT_HEAD = IM_START + "assistant\n"

# 关思考时，模型自己的 chat_template 在 `add_generation_prompt` 里要补的那一截
# （`enable_thinking=false` 分支渲出来就是这对**空**标记）。
#
# 为什么非得补：` /no_think` 只是**软**提示。2026-09-18 板上实测，"要组织语言"的题
# （`介绍一下杭州` / `写一首关于秋天的诗`）在关思考下会先写一整段
# `<think>Thinking Process:…`，正文里甚至有一句 `* Constraint: /no_think (This means
# I should no…`——它看见了提示、权衡了一下，然后照开不误。256 token 一截，`</think>`
# 没机会吐出来，ThinkStripper 只能按"宁可露标签也不静默吞内容"把整段推理原样发给
# 客户端，屏幕上就是一堵英文推理墙（用户报的"网页端某一格固定出现思考模式"）。
#
# 补上它 = 模型从 `</think>` **后面**开始生成，结构上开不了 think 段。代价别当成顺手
# 加的一行：这段文本今天是被模型**生成**出来、再被网关摘掉的，改成预填后同样的回答
# 少生成 ~4 个 token（略快），但 prompt 长度与 `completion_tokens` 都变了——
# 2026-09-18 之前量到的吞吐数字**不能与之后的逐字节互比**。收益见 toolcalls.py 顶部：
# 那两处"刻意偏差"（助手轮的推理包装、生成尾巴）在关思考下就此消掉，`known`
# 这笔 KV 记账也从"近似"变成"精确"。
THINK_OFF = "<think>\n\n</think>\n\n"


# ===========================================================================
# 1. 后端进程与帧协议
# ===========================================================================

class BackendError(Exception):
    """后端给出的一次失败。

    `soft` 区分两种失败，因为对客户端的语义完全不同：
      · soft=False（默认）——后端出了问题，或者会话的驱动线程已经死了。503，
        客户端该重试；会话同时被标死（见 SessionPool.mark_dead）。
      · soft=True——**这一轮被后端拒了，但会话是好的**（目前只有"上下文装不下"，
        见 main.cc 的 REJECT 帧）。这是客户端要改请求的错误（历史太长），所以回 400
        而不是 503——503 会让客户端原样重试，而原样重试必然再次被拒。
    """

    def __init__(self, message, soft=False):
        Exception.__init__(self, message)
        self.soft = soft


class _Request(object):
    """一次在飞的请求。reader 线程按 rid 把帧丢进来。"""

    def __init__(self, rid):
        self.rid = rid
        self.raw = bytearray()
        # 增量解码器：TokenToPiece 可能把一个 UTF-8 字的一切两半（多字节字符被切成
        # 两个 token），逐帧 decode 会在切缝处吐出替换字符。非流式路径整段 decode，
        # 流式路径必须用增量解码器。
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.done = threading.Event()
        self.error = None
        self.session = -1
        self.finish_reason = None
        self.prefill_tokens = 0
        self.decode_tokens = 0
        self.prefill_ms = 0.0
        self.decode_ms = 0.0
        self.context_tokens = 0
        self.was_cleared = False        # 后端在本轮里清过 KV（上下文将满 / RESET）
        self.rejected = False           # 后端在 prefill 前拒了这一轮（会话没死，见 REJECT）
        self.counted_tokens = None      # COUNT 帧的答复（精确 token 数；<0 = 数不出来）
        self.op_ok = None               # SAVED/LOADED 帧的答复（KV 换页成没成，改动 C）
        self.on_delta = None            # 流式回调；在 reader 线程里被调用
        self.dead = False               # 后端进程没了
        self.kv = None                  # 会话池本轮的复用决策，用于 X-KV-Reuse 头
        # 「谁把这一段字节交给客户端」的账本。reader 线程（有帧就投）和 HTTP 线程
        # （接手时把等待期间攒下的补发）是两条线程，两边都要原子地决定这件事：
        # `delivered` 是 raw 里**已经交出去**的字节数，两边都在 deliver_lock 里推进它，
        # 于是每个字节恰好交一次（重复投递和整段重发都由此消失），且解码器按字节
        # 顺序被喂——跨帧切开的 UTF-8 字符才拼得回来。
        self.delivered = 0
        self.deliver_lock = threading.Lock()
        # 客户端在流式过程中断开了（写响应失败）。一旦置位就**立刻叫停后端**，
        # 别让板卡把整段 decode 跑完——否则 Claude 那边早就结束了，NPU 还在空转。
        self.client_gone = False
        # ---- 性能统计（2026-09-29 加，调度算法 A/B 用）----
        # 三个单调时刻把一次请求的时延拆开：
        #   t_submit      REQ/RESUME 帧写进后端 stdin 的时刻（**排队之后**，lease 已拿到）
        #   t_first_token 第一段**非空**文本到达（≈ prefill 完成 + 第一个解码步）
        #   t_done        DONE / ERR / REJECT 帧到达
        # 排队等待不在这里——它在 q.kv["wait"]（会话池记账），两者相加才是客户端看到的总时延。
        self.t_submit = None
        self.t_first_token = None
        self.t_done = None

    def ttft_ms(self):
        """首 token 时延（服务器侧）：REQ 写出 → 第一段非空文本。不含排队。"""
        if self.t_submit is None or self.t_first_token is None:
            return None
        return round((self.t_first_token - self.t_submit) * 1000.0, 3)

    def e2e_ms(self):
        """单轮端到端（服务器侧）：REQ 写出 → DONE。不含排队。"""
        if self.t_submit is None or self.t_done is None:
            return None
        return round((self.t_done - self.t_submit) * 1000.0, 3)

    def text(self):
        return self.raw.decode("utf-8", "replace")


def _argv_int(argv, name):
    """从后端命令行里取一个整数选项（`--ctx-size 8192` 或 `--ctx-size=8192`）。

    要它是为了 `/health` 能报出 `ctx_size`：**窗口数上限是上下文长度决定的**
    （KV 按"每路满上下文"预分配，ctx 越大每路越贵、能开的窗口越少），页面不拿到
    这个数就只能写死一个，换一份导出就变成骗人。

    解析**真实 argv** 而不是另存一份配置：子进程真正收到的那个值才算数。
    取不到/不是整数就回 None——宁可让页面少显示一格，也不要编一个数出来。
    """
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith(name + "="):
            val = a.split("=", 1)[1]
        else:
            continue
        try:
            return int(val)
        except ValueError:
            return None
    return None


# ===========================================================================
# 资源观测：RK3588 这边网关自己读，RK1828 那边问后端（STAT 帧）
# ===========================================================================
#
# 为什么分两半：**只有持有设备的那个进程能查 RK1828**。四张卡是 PCIe 设备（driver
# `rkep`），主机侧既没有 hwmon 也没有 thermal_zone，`rknn-smi` 在后端驻留时必然是
# `Failed to initialize rknnsmi`（2026-09-20 实测；那个"还能出数"的 `rknn-smi info -w`
# 是它在后端起来**之前**就抢住了设备）。所以每卡设备内存只能由后端用
# `RKNN3_QUERY_DEVICE_MEM_INFO` 采，经 STAT 帧送过来。
#
# 而 RK3588 的一切都在 procfs/sysfs 里，网关直接读——不必为它绕一圈帧协议。

STAT_MIN_INTERVAL = 0.9     # 两次 STAT 之间至少隔这么久（页面约 2s 轮一次，够用）
# 发出 STAT 后最多等这么久。后端是**就地答原子量**、不碰设备也不进队列，正常 <1ms
# （板上实测：同一台机器连着问 10 次，HTTP 全程 7~11ms）。但 2026-09-20 在板上量到
# **偶发**的答复会晚于 0.35s——那版把 wait 设成 0.35，结果 /v1/system 每隔几次就拿到
# 上一份 doc，busy_pct 由 0.0 变成 None，页面上就在数字和 "—" 之间闪。
# 所以放成 1.0：页面轮询是 2s，多等这 0.65s 谁也不影响（这条路径只服务 /v1/system，
# 不在聊天请求的路上）；而"等不到就返回旧的"这条兜底照旧在——后端真卡住时它仍然
# 按时返回、只把年龄标出来。
STAT_WAIT = 1.0

# /v1/system 的快照缓存：**1 秒内不管几个人来问，只采一次**。
#
# 为什么要有它：一次快照 = 读十几个 procfs/sysfs 文件 + 一次 STAT 往返。这些读数和四路
# 推理抢的是同一台 RK3588、同一条 DDR 带宽——多开几个页面就会变成"看的人越多、被看的那
# 台机器越慢"。而这一块要看的都是**慢变量**（内存、温度、谁在占着会话），1 秒的陈旧谁也
# 看不出差别；页面自己也是 2s/6s 轮一次。
# 顺带把"同一秒里的两份 doc 完全一样"变成必然：页面靠 ts 判断要不要重画，重复的那一份
# 连 DOM 都不动（见 demo_4chat.html 的 refreshSystem）。
# 传 max_age=0 可以绕开缓存强制重采（自检里用它来量"缓存到底有没有生效"）。
SYS_CACHE_S = 1.0

_THERMAL_BASE = "sys/class/thermal"


def _read_proc_stat(path):
    """{'cpu': (busy, total), 'cpu0': …, …}，单位 jiffies。

    busy = total − idle − iowait。iowait 算空闲：它等的是磁盘，不是 CPU 在干活。
    """
    out = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                if not line.startswith("cpu"):
                    break
                f = line.split()
                try:
                    vals = [int(x) for x in f[1:]]
                except ValueError:
                    continue
                if len(vals) < 5:
                    continue
                idle = vals[3] + vals[4]
                out[f[0]] = (sum(vals) - idle, sum(vals))
    except (OSError, ValueError):
        return {}
    return out


def _read_meminfo(path):
    want = {"MemTotal": "total", "MemFree": "free", "MemAvailable": "available",
            "Buffers": "buffers", "Cached": "cached", "SReclaimable": "sreclaimable",
            "SwapTotal": "swap_total", "SwapFree": "swap_free"}
    out = {}
    try:
        with open(path, "r") as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 2:
                    key = want.get(f[0].rstrip(":"))
                    if key:
                        out[key] = int(f[1]) * 1024
    except (OSError, ValueError):
        return {}
    return out


# 「有哪些热区、各自叫什么」是**开机就定下来的**，问一次就够。以前每一拍都重新
# listdir 一遍、再把每个区的 type 读一遍——板卡上 7 个区就是 1 + 7 次 sysfs 打开，
# 每拍都做，而它们连一个字节都不会变。现在只留"读温度"这一半（7 次）。
# 只缓存**列成功**的结果：本机（没有 /sys）那种读不到的情况不缓存，免得偶然一次失败
# 被记成"这台机器没有热区"。
_THERMAL_ZONES = {}                     # base 路径 -> [(zone_dir_name, label), …]
_THERMAL_ZONES_LOCK = threading.Lock()


def _thermal_zones(base):
    with _THERMAL_ZONES_LOCK:
        hit = _THERMAL_ZONES.get(base)
    if hit is not None:
        return hit
    zones = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return zones
    for name in names:
        if not name.startswith("thermal_zone"):
            continue
        try:
            with open(os.path.join(base, name, "type"), "r") as fh:
                label = fh.read().strip()
        except (OSError, ValueError):
            continue
        zones.append((name, label))
    with _THERMAL_ZONES_LOCK:
        _THERMAL_ZONES[base] = zones
    return zones


def _read_thermals(base):
    """板上 7 路热区。**全是 RK3588 自己的**（含它自己那个 npu-thermal），
    RK1828 四张卡一个都不在里面——这一点别在页面上含糊掉。"""
    out = []
    for name, label in _thermal_zones(base):
        try:
            with open(os.path.join(base, name, "temp"), "r") as fh:
                milli = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        out.append({"zone": name, "label": label, "c": round(milli / 1000.0, 1)})
    return out


# RK1828 四张卡的结温**不在这台机器的 sysfs 里**（卡是 PCIe 设备，驱动 rkep，
# /sys/class/hwmon 下那 9 个读数全是 RK3588 自己的），只能走 `rknn-smi` 经 PCIe 问卡。
# 而 rknn-smi 要 root（/dev/pcie-rkep-* 是 crw------- root root），所以那一步拆成了
# 一个独立的小进程：`rknn_smi_temp.py`（+ `rknn-temp.service`）以 root 每 5 秒采一次，
# 原子替换 `/run/rknn-temp.json`；**网关这边不提权，只读这个文件**。
#
# 为什么不让网关自己跑 rknn-smi：网关是局域网上无鉴权的常驻服务（见 CHANGELOG
# 2026-09-20 那条），给它开一条免密 sudo 等于把"能拉起 root 进程"变成它能做的事，
# 换来的只是省掉一个 135ms/5s 的只读进程。不合算。
#
# ⚠️ 文件读不到 = 那个服务没装/没跑，页面上画 "—"，**不影响任何别的功能**。
_CARD_TEMP_FILE = "/run/rknn-temp.json"


def _read_card_temps(path):
    """-> ({device_id: 摄氏度}, 采样时刻 epoch 秒)。

    读不到就是 `({}, None)`——**不是 0**。0°C 和"没有这个读数"在屏幕上必须是两句话
    （和 fmtPct 那条同一个道理；温度尤其不能默认成 0，那是在说"这张卡凉快"）。
    """
    try:
        with open(path, "r") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}, None
    if not isinstance(doc, dict):
        return {}, None
    out = {}
    for d in (doc.get("devices") or []):
        if not isinstance(d, dict):
            continue
        did, c = d.get("id"), d.get("c")
        if isinstance(did, int) and isinstance(c, (int, float)) and not isinstance(c, bool):
            out[did] = float(c)
    ts = doc.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return out, float(ts)
    return out, None


class HostSampler(object):
    """RK3588 的 CPU / 内存 / 温度。没有依赖，也不碰 NPU。

    CPU 占用率是**两次采样之差**（和 `top` 同口径）。所以第一次调用没有"上一次"，
    `pct` 是 None 而不是 0.0——"还没量到"和"占用 0%"在屏幕上必须是两句话，
    否则页面刚打开那一瞬间会显示一个假的 0%。

    `root` 是给测试用的：把 `/proc`、`/sys` 换成一个夹具目录，本机（Windows，没有
    /proc）就能把**这段解析与百分比算术**真跑一遍。板卡上传默认的 `/`。
    读不到文件时每一项都退化成空/None，不抛——观测端点不该因为读不到一个文件而 500。
    """

    def __init__(self, root="/"):
        self.root = root
        self._lock = threading.Lock()
        self._prev = None          # (monotonic, {key: (busy, total)})

    def _p(self, rel):
        return os.path.join(self.root, rel)

    def sample(self):
        now = time.monotonic()
        cur = _read_proc_stat(self._p("proc/stat"))
        with self._lock:
            prev = self._prev
            self._prev = (now, cur)

        def pct(key):
            if not prev or not cur or key not in cur or key not in prev[1]:
                return None
            pb, pt = prev[1][key]
            cb, ct = cur[key]
            d_total = ct - pt
            if d_total <= 0:
                return None
            cpu = (cb - pb) * 100.0 / d_total
            return round(max(0.0, min(100.0, cpu)), 1)

        mem = _read_meminfo(self._p("proc/meminfo"))
        mem_out = dict(mem)
        if mem.get("total"):
            # MemAvailable 才是"还能拿来用的"，MemFree 不含可回收的页缓存。用 MemFree
            # 会把占用率显示得偏高（板上 16 GB 的机器上差 1 GB 量级）。
            usable = mem.get("available", mem.get("free", 0))
            mem_out["used"] = max(0, mem["total"] - usable)
            mem_out["used_pct"] = round(mem_out["used"] * 100.0 / mem["total"], 1)

        load = []
        try:
            with open(self._p("proc/loadavg"), "r") as fh:
                load = [float(x) for x in fh.read().split()[:3]]
        except (OSError, ValueError):
            pass

        uptime = None
        try:
            with open(self._p("proc/uptime"), "r") as fh:
                uptime = float(fh.read().split()[0])
        except (OSError, ValueError):
            pass

        cores = sorted([k for k in cur if k != "cpu"],
                       key=lambda s: int(s[3:]) if s[3:].isdigit() else 0)
        return {
            "cpu": {
                "n": len(cores) or None,
                "pct": pct("cpu"),
                "per_core": [pct(k) for k in cores],
                "loadavg": load,
            },
            "mem": mem_out,
            "thermal": _read_thermals(self._p(_THERMAL_BASE)),
            "uptime_s": round(uptime, 1) if uptime is not None else None,
        }


class CardStats(object):
    """把后端 STAT 帧变成每张卡的"现在"。

    NPU 利用率 = **Δ忙时 / Δ墙上时间**，两个数都取后端自己的单调时钟（`now_us`），
    所以不受网关这边调度抖动影响。忙时由后端在卡级锁内累加，量的是"这张卡真在算"
    的时间，不是"有没有人在等"。

    第一次调用没有"上一次"，`busy_pct` 是 None——**不是 0%**。这和 HostSampler 是
    同一个道理：刚打开页面就显示"NPU 0%"，会让人以为板卡闲着。

    同一份 doc 被连着采两次（后端那边 `now_us` 没动）时：不推进窗口，并把上一次算出的
    百分比原样再给一次。**多个看页面的人同时轮询时每个请求都会走到这条路上**——窗口
    是"上一次真拿到新 doc 到现在"这一段，谁先问谁算，后面的人拿到同一个数（而不是 "—"，
    也不是一个被切短的窗口算出来的数）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._prev = None          # (now_us, {card_name: busy_us})
        self._last_pct = {}        # card_name -> 上一次算出来的利用率（同一份 doc 直接复用它）

    def sample(self, doc, temps=None, temps_at=None):
        """`temps` = {device_id: 摄氏度}、`temps_at` = 采样时刻（都来自 rknn-smi 那个文件）。

        **按 stage 序号对上 device 序号**：后端的 `stages[i].name` 就是
        `"stage" + i`（main.cc），而 STAT 帧里 `cards` 是按 `stages` 的顺序 push 的，
        所以第 i 张卡就是 `stage{i}`、也就是 `rknn-smi` 里的 Device i。
        这个对应 2026-09-20 用一条独立的来路核过：四张卡里 stage3 的内存是唯一的异类
        （95.4% vs 92.5%），而 smi 那边 Device 3 也正好是唯一的异类（4866 vs 4714 MB）。
        """
        if not isinstance(doc, dict):
            return {"ok": False, "cards": [], "busy_pct": None, "temp_age_s": None}
        now_us = doc.get("now_us") or 0
        raw = doc.get("cards") or []
        busy = {}
        for c in raw:
            if isinstance(c, dict) and c.get("name"):
                busy[c["name"]] = c.get("busy_us") or 0
        with self._lock:
            prev = self._prev
            # ⚠️ 同一份 doc 连着被采两次（now_us 没动）时**不推进 _prev**，而且下面
            # 把上一次算出来的百分比原样再给一次。这道"不推进"是 2026-09-20 在板上
            # 量出来的：STAT 的答复偶尔会晚于 STAT_WAIT（0.35s 那版实测过 0.3s 还没到），
            # 超时的调用拿到的是**上一份** doc ⇒ Δt=0 ⇒ 算不出比值 ⇒ 页面在 0% 和 "—"
            # 之间闪。若这时把 _prev 挪到这份旧 doc 上，下一份真 doc 的 Δ 还只剩下
            # 后半段，等于把一段观测悄悄切掉。两个毛病一起修：拿不到新 doc 就
            # **既不动窗口、也照旧显示上一次的值**（值本身没变旧，它是同一份数据算的）。
            if prev is None or not prev[0] or now_us > prev[0]:
                self._prev = (now_us, busy)
                repeated = False
            else:
                repeated = True

        # rknn-smi 那个文件的年龄。**它是整份文件的属性、不是某一张卡的**，所以只在
        # npu 这一层说一次；抄进四张卡里只会给"它们各自变旧"留出可能。
        # 时钟往回跳（NTP 校时）时可能算出负数——夹成 0，别让页面显示 "-3.2s 前"。
        temp_age = None
        if temps_at:
            temp_age = round(max(0.0, time.time() - temps_at), 1)

        cards = []
        pcts = []
        for pos, c in enumerate(raw):
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            mem_at = c.get("mem_at_us") or 0
            total = c.get("mem_total") or 0
            free = c.get("mem_free") or 0
            # 这张卡对上 `rknn-smi` 里的哪一个 Device（理由见 docstring）。认不出
            # `stage<i>` 这种名字就不猜，直接用它在列表里的位置。
            m = re.match(r"^stage(\d+)$", name or "")
            idx = int(m.group(1)) if m else pos
            card = {
                "name": name,
                "ctx_len": c.get("ctx_len"),
                "run_calls": c.get("run_calls"),
                "mem_total": total,
                "mem_free": free,
                "mem_used": max(0, total - free) if total else None,
                "mem_used_pct": (round(max(0.0, total - free) * 100.0 / total, 1)
                                 if total else None),
                "node_num": c.get("node_num") or None,
                "node_min_free": c.get("node_min_free") or None,
                # 快照年龄。mem_at_us == 0 表示后端**一次都没采到**（查询失败），
                # 那和"采过但很旧"是两回事，得让页面分开说。
                "mem_age_s": (round((now_us - mem_at) / 1e6, 1)
                              if (mem_at and now_us >= mem_at) else None),
                "busy_pct": None,
                # 结温。`temps` 读不到时是 None ⇒ 页面画 "—"（**不是 0**）。
                "temp_c": (temps or {}).get(idx),
            }
            if repeated:
                # 这次是同一份 doc：百分比没得算，但**数据本身没变旧**，把上一次算的
                # 原样给回去，别让页面在数字和 "—" 之间闪（"—" 要留给"真的没有值"）。
                with self._lock:
                    card["busy_pct"] = self._last_pct.get(name)
            else:
                p0 = prev[1].get(name) if prev else None
                p1 = busy.get(name)
                # ⚠️ `now_us > prev[0]` 这一条是**除数保护**，不是多余的条件：后端若发来
                # 一份 `now_us` 缺失/为 0 的 STAT（`doc.get("now_us") or 0` 会把它当 0），
                # 上面 `not prev[0]` 那一支会一直把 `_prev` 重置成 `(0, busy)`，
                # 于是 Δ墙钟 = 0 ⇒ ZeroDivisionError ⇒ **整个 /v1/system 500**。
                # 观测端点因为后端一个字段没填就整块打不开，是最不该发生的那类故障；
                # 算不出比值就老老实实给 None（页面上画 "—"）。
                if p0 is not None and p1 is not None and p1 >= p0 and now_us > prev[0]:
                    span = now_us - prev[0]
                    card["busy_pct"] = round(max(0.0, min(100.0,
                                                         (p1 - p0) * 100.0 / span)), 1)
                if card["busy_pct"] is not None:
                    with self._lock:
                        self._last_pct[name] = card["busy_pct"]
            if card["busy_pct"] is not None:
                pcts.append(card["busy_pct"])
            cards.append(card)
        return {
            "ok": True,
            "cards": cards,
            # 四张卡是一条流水线，同一时刻大致同样忙；取平均当"整机 NPU 利用率"。
            "busy_pct": round(sum(pcts) / len(pcts), 1) if pcts else None,
            # 卡温那一份文件有多旧（秒）。None = 没有那个文件（采集服务没装/没跑）。
            "temp_age_s": temp_age,
        }


class Backend(object):
    """demo 子进程 + 帧协议。所有方法都线程安全。"""

    def __init__(self, argv, log_path, frame_fd=None):
        self.argv = argv
        self.log_path = log_path
        self.ctx_size = _argv_int(argv, "--ctx-size")
        # 后端 READY 帧报来的**真实生效上下文**（模型内建 max_ctx_len）。窗口滚动的预算
        # 优先用它：--ctx-size 会被 runtime 就近夹住（32768 导出请求 8192 仍拿到 32768），
        # 照 argv 算会算错。老后端/桩不发这个字段时保持 None，回退到 ctx_size。
        self.model_ctx_size = None
        # 协议 v2 标志：后端 READY 是否带了第三个字段（= 认识 COUNT / SAVE / LOAD）。
        # 旧后端只发两个字段。**这个标志是防呆的关键**：网关若往一个不认识 COUNT 的旧
        # 后端发 COUNT，会一直等不到答复（帧流里回的是一条 rid=0 的 bad header ERR），
        # 每条请求都卡满 COUNT_TIMEOUT，客户端侧就是"502/上游连接失败"。
        self.protocol_v2 = False
        # None  → 正常路径：自己开管道，用 pass_fds 把写端交给子进程，并追加
        #         --serve-fd <号> 告诉它认哪个号（Linux 板卡上走这条）。
        # "stdout" → 帧从子进程的 stdout 读。这条是给**本地自检的桩后端**用的：
        #         pass_fds 在 Windows 上直接不支持（实测 AssertionError），
        #         没有它就没法在本地把网关这半边验掉，每次试错都要等板卡上 230s
        #         的模型加载。顺带它也覆盖了 C++ 侧"该 fd 打不开就退回 stdout"的情形。
        self.frame_fd = frame_fd
        self.proc = None
        self.sessions = 0
        self.default_max_new_tokens = DEFAULT_MAX_NEW_TOKENS
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()   # 帧写 stdin 必须整体持锁，见 submit()
        self._reqs = {}
        self._next_rid = 1
        self._ready = threading.Event()
        self._dead = False
        self.stopping = False                 # 我们自己发的 QUIT，用来区分"正常收工"与"后端死了"
        self._rfile = None
        self._reader = None
        self.on_clear = None                  # 回调 (session, ctx, limit)
        # 资源观测：最近一次的 STAT 答复 + 它到达的时刻。读线程写、HTTP 线程读，
        # 用一把 cond 同时当锁和"新的答复到了"的信号（见 request_stats）。
        self.stats = None
        self.stats_seq = 0
        self.stats_at = 0.0
        self._stats_cv = threading.Condition()
        self._stats_sent_at = 0.0

    # ---- 生命周期 ----

    def start(self):
        logf = open(self.log_path, "wb")
        if self.frame_fd == "stdout":
            # 桩模式：帧走子进程的 stdout，日志走 stderr→文件。
            self.proc = subprocess.Popen(list(self.argv), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=logf)
            logf.close()
            self._rfile = self.proc.stdout
        else:
            r, w = os.pipe()
            # 子进程按 --serve-fd 认这个号：pass_fds 保留父进程里的 fd 号，所以直接把 w
            # 告诉它，不需要 dup2 硬凑成 3（那在多线程下不安全）。
            argv = list(self.argv) + ["--serve-fd", str(w)]
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=logf,
                                         stderr=logf, pass_fds=(w,))
            os.close(w)  # 父进程必须关掉写端，否则读端在子进程退出后也等不到 EOF
            logf.close()
            self._rfile = os.fdopen(r, "rb")
        self._reader = threading.Thread(target=self._read_loop, name="frame-reader")
        self._reader.daemon = True
        self._reader.start()
        if not self._ready.wait(BACKEND_READY_TIMEOUT):
            raise BackendError("backend did not send READY within %.0fs"
                               % BACKEND_READY_TIMEOUT)
        if self._dead:
            raise BackendError("backend exited during startup; see %s" % self.log_path)

    def alive(self):
        return (not self._dead) and self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.stopping = True
                self.proc.stdin.write(b"QUIT\n")
                self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    # ---- 读帧 ----

    def _read_loop(self):
        try:
            while True:
                line = self._rfile.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line:
                    continue
                tag, _, rest = line.partition(b" ")
                if tag == b"READY":
                    f = rest.split()
                    self.sessions = int(f[0])
                    self.default_max_new_tokens = int(f[1])
                    # 第三个字段（可选，2026-09 起）：真实生效的上下文上限。老后端不发，
                    # 保持 None，窗口预算回退到 argv 的 --ctx-size。它的存在与否同时是
                    # "后端是否支持 COUNT/SAVE/LOAD 新帧"的能力标志（见 protocol_v2）。
                    if len(f) >= 3:
                        self.model_ctx_size = int(f[2])
                        self.protocol_v2 = True
                    self._ready.set()
                elif tag in (b"DELTA", b"ERR", b"REJECT"):
                    # DELTA 是 `DELTA <rid> <n>`，ERR/REJECT 多一个 session 字段：
                    # `ERR <rid> <session> <n>`、`REJECT <rid> <session> <n>`
                    # （写侧见 src/main.cc 的 serve_err / serve_reject）。
                    # **两个不能共用一套解析**：按 DELTA 切第一刀的话，ERR 的 n 会拿到
                    # "<session> <n>"，int() 抛异常 → 读线程死 → 所有在途请求被判
                    # "backend exited" → 之后每个请求都 503，直到重启网关。一次"某轮
                    # 失败"就能把 4 会话的服务整体打掉，所以这里按字段位置取：
                    # 两种帧的 rid 都是第一个字段、n 都是最后一个字段。
                    # （ERR 的 session 这里用不上：请求自己知道落在哪个会话上。）
                    f = rest.split()
                    n = int(f[-1])
                    payload = self._rfile.read(n) if n > 0 else b""
                    self._dispatch_payload(tag, int(f[0]), payload)
                elif tag == b"STAT":
                    # `STAT <n>` + JSON 载荷，没有 rid（它不是某个请求的答复）。
                    # ⚠️ 这一帧**必须在这里被消费掉**：不认它的 tag 就读不走那 n 个
                    # 字节，整条帧流从此错位——和 ERR 帧那次是同一个坑（§9.7 末）。
                    f = rest.split()
                    n = int(f[0]) if f else 0
                    payload = self._rfile.read(n) if n > 0 else b""
                    self._on_stats(payload)
                elif tag == b"COUNTED":
                    # `COUNTED <rid> <tokens>`，无载荷。数的是窗口裁剪要用的精确 token 数
                    # （改动 A）。会话数是 0 也没关系：它不占会话、不进调度队列。
                    f = rest.split()
                    q = self._get(int(f[0]))
                    if q is not None:
                        q.counted_tokens = int(f[1])
                        q.done.set()
                elif tag in (b"SAVED", b"LOADED"):
                    # `SAVED <rid> <ok>` / `LOADED <rid> <ok>`，无载荷（改动 C）。
                    f = rest.split()
                    q = self._get(int(f[0]))
                    if q is not None:
                        q.op_ok = (int(f[1]) != 0)
                        q.done.set()
                elif tag == b"DONE":
                    f = rest.split()
                    self._handle_done(f)
                elif tag == b"CLEAR":
                    f = rest.split()
                    rid, session, ctx, limit = int(f[0]), int(f[1]), int(f[2]), int(f[3])
                    if self.on_clear:
                        self.on_clear(session, ctx, limit)
                    q = self._get(rid)
                    if q is not None:
                        q.was_cleared = True
        except Exception as exc:                       # noqa: BLE001 - 读线程不能把异常吞成静默死锁
            sys.stderr.write("[gateway] frame reader failed: %r\n" % (exc,))
        finally:
            self._dead = True
            self._ready.set()
            # 后端没了 = 这个网关从此刻起只能回 503（池子里再等也不会多出会话来）。
            # 必须吵一声：现场除了这一行，其余症状全是"客户端卡住/503"，看不出是谁先死的。
            # 自己发 QUIT 收工的那条路（stopping）不算故障，别误报。
            if not self.stopping:
                rc = None
                try:
                    rc = self.proc.poll()
                except Exception:                      # noqa: BLE001
                    rc = None
                sys.stderr.write("[gateway] backend exited (returncode=%r, frame stream "
                                 "ended); every request fails with 503 until the gateway "
                                 "is restarted\n" % (rc,))
                sys.stderr.flush()
            with self._lock:
                pending = list(self._reqs.values())
            for q in pending:
                q.dead = True
                q.error = q.error or "backend exited"
                q.done.set()

    def _on_stats(self, payload):
        """读线程收到一帧 STAT：解出 JSON 放进缓存，并叫醒等它的人。

        解析失败**只丢这一帧**，不往上抛：帧流里出现一个解不开的 STAT 不应该把读线程
        带走（读线程一死，所有在途请求都被判 "backend exited"，整个服务变 503）。
        """
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            sys.stderr.write("[gateway] bad STAT payload: %r\n" % (exc,))
            return
        with self._stats_cv:
            self.stats = doc
            self.stats_at = time.monotonic()
            self.stats_seq += 1
            self._stats_cv.notify_all()

    def request_stats(self, min_interval=STAT_MIN_INTERVAL, wait=STAT_WAIT):
        """要一份最新的后端资源快照。**永远不会抛异常，也永远不长时间阻塞。**

        两道节制：
        · `min_interval` 之内问过就直接给缓存——STAT 和 REQ 走同一条 stdin，
          没人在看页面时一个字节都不该发，有人在看时也不该比页面刷新还快；
        · 发出去之后最多等 `wait` 秒。后端是**就地答原子量**（不排队、不碰设备），
          正常 <1ms；等不到说明后端卡住了，那就返回上一次的缓存并让页面自己标旧
          ——**卡住的时候返回旧数比返回 500 有用得多**，看护恰恰要看这个。
        """
        with self._stats_cv:
            now = time.monotonic()
            if now - self._stats_sent_at < min_interval:
                return self.stats
            if self._dead or self.proc is None or self.proc.stdin is None:
                return self.stats
            want = self.stats_seq + 1
            self._stats_sent_at = now
            try:
                with self._write_lock:
                    self.proc.stdin.write(b"STAT\n")
                    self.proc.stdin.flush()
            except Exception:                              # noqa: BLE001
                # 写失败就是后端没了/管道断了。这条路径**不能抛**：/v1/system 是观测
                # 端点，它挂掉不该把看页面的人也带走。
                return self.stats
            deadline = now + wait
            while self.stats_seq < want:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._stats_cv.wait(left)
            return self.stats

    def _get(self, rid):
        with self._lock:
            return self._reqs.get(rid)

    def _dispatch_payload(self, tag, rid, payload):
        q = self._get(rid)
        if q is None:
            return          # 客户端已经放弃这个 rid 了，丢掉帧即可
        if tag == b"DELTA":
            # 与 HTTP 线程的"接手补发"互斥：要么这一段由我投递（delivered 随之推进），
            # 要么它还留在 raw 里等着被补发，两者只能发生一件。
            with q.deliver_lock:
                q.raw.extend(payload)
                if q.on_delta is None:
                    return          # 还没人接手（HTTP 线程尚未走到 attach）：先攒着
                chunk = q.decoder.decode(bytes(payload))
                q.delivered += len(payload)
                cb = q.on_delta
            if chunk:
                # TTFT 的终点：**第一段非空文本**到达。空的 DELTA（多字节字符被切开、
                # 或后端只读了半截）不算出字，所以放在 chunk 非空判断里面。
                if q.t_first_token is None:
                    q.t_first_token = time.monotonic()
                try:
                    cb(chunk)
                except Exception:
                    # 客户端断开了：立刻叫停后端，别让它继续 decode（板卡不再空转），
                    # 会话也能尽快释放给排队者。STOP 是幂等的（后端对未知 rid 静默）。
                    q.client_gone = True
                    q.on_delta = None
                    self.stop_request(q.rid)
        elif tag == b"REJECT":
            # 后端在 prefill **之前**拒了这一轮（上下文装不下）：会话还活着，只是这一轮
            # 一个 token 都没跑。所以只失败这一条请求，**绝不能标死这个会话**。
            q.rejected = True
            q.error = payload.decode("utf-8", "replace")
            q.t_done = time.monotonic()
            q.done.set()
        else:               # ERR
            q.error = payload.decode("utf-8", "replace")
            q.t_done = time.monotonic()
            q.done.set()

    def _handle_done(self, f):
        if len(f) < 8:
            # 字段不齐的 DONE **不能静默 return**：这个请求从此既没有 DONE 也没有 ERR，
            # 会一直挂在 HTTP 线程的 q.done.wait(REQUEST_TIMEOUT) 上（默认 1800s），
            # 期间白占着一个会话租约——别的对话只能排队等它超时。按 rid 判它失败，
            # 让客户端立刻拿到一个能看懂的错误，同时读线程照常活着（不牵连其他请求）。
            try:
                rid = int(f[0])
            except (IndexError, ValueError):
                sys.stderr.write("[gateway] malformed DONE frame: %r\n" % (f,))
                return
            q = self._get(rid)
            if q is not None and not q.done.is_set():
                q.error = "malformed DONE frame from backend: %r" % (f,)
                q.t_done = time.monotonic()
                q.done.set()
            return
        rid = int(f[0])
        q = self._get(rid)
        if q is None:
            return
        q.session = int(f[1])
        # 头行是 ASCII 字节串：必须 decode 成 str，否则它会被原样塞进 JSON 里，
        # json.dumps 对 bytes 直接抛 TypeError（非流式路径必崩）。
        q.finish_reason = f[2].decode("ascii", "replace")
        q.prefill_tokens = int(f[3])
        q.decode_tokens = int(f[4])
        q.prefill_ms = float(f[5])
        q.decode_ms = float(f[6])
        q.context_tokens = int(f[7])
        if q.on_delta is not None:
            tail = q.decoder.decode(b"", True)
            if tail:
                if q.t_first_token is None:
                    q.t_first_token = time.monotonic()
                try:
                    q.on_delta(tail)
                except Exception:
                    q.on_delta = None
        q.t_done = time.monotonic()
        q.done.set()

    # ---- 提交请求 ----

    def submit(self, prompt, max_new_tokens, session=-1, reset=False):
        if self._dead:
            raise BackendError("backend exited")
        payload = prompt.encode("utf-8")
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            q = _Request(rid)
            self._reqs[rid] = q
        header = "REQ %d %d %d %d %d\n" % (rid, session, max_new_tokens,
                                           1 if reset else 0, len(payload))
        # 必须整体持锁一次性写出：头行与载荷是两条独立的 write，中间被另一条 HTTP
        # 线程插进它自己的头行，帧流立刻就错位了。
        with self._write_lock:
            try:
                self.proc.stdin.write(header.encode("ascii") + payload)
                self.proc.stdin.flush()
            except Exception as exc:
                with self._lock:
                    self._reqs.pop(rid, None)
                raise BackendError("failed to write request: %r" % (exc,))
        q.t_submit = time.monotonic()
        return q

    def stop_request(self, rid):
        """停掉在跑的那一轮（网页「暂停回答」的后端入口）。

        只写一帧 `STOP <rid>` 就返回，**不等结果**：等那一轮跑完再回等于没停。
        后端收到后会在下一个 token 边界停手、照发一帧 DONE（finish_reason=abort），
        那一帧由**它自己的**流式响应收尾——所以这里不需要、也没法回一个「停到了没有」。
        rid 不认识时后端是静默的（见 main.cc 里 STOP 那段的注释），所以这里对
        「这一轮根本不在跑」也照样回 200：有没有停到，从 DONE 的 finish_reason 看得到。
        """
        if self._dead or self.proc is None or self.proc.stdin is None:
            return
        try:
            # 和 submit 同一个理由：头行是一次完整的 write，与别的 HTTP 线程并发的
            # REQ/STAT 之间不能交错（STOP 没有载荷，其实比 REQ 更不容易错位，但
            # 持锁写是这里一贯的规矩，保持一致）。
            with self._write_lock:
                self.proc.stdin.write(b"STOP %d\n" % rid)
                self.proc.stdin.flush()
        except Exception:                              # noqa: BLE001
            # 写失败就是后端没了。STOP 是「尽力而为」的旁路，不值得为它把请求炸掉——
            # 后端真没了，那一轮的 DONE 也不会到，页面自然走超时/断连那条路。
            pass

    def resume(self, session, max_new_tokens):
        """继续上一轮被打断的 decode（网页「继续回答」）。

        写一帧 `RESUME <rid> <session> <max_new_tokens>`，**不带载荷**：KV 已经装着
        上一轮 STOP 时的状态，RESUME 只是让后端跳过 prefill 接着 decode。返回的
        `_Request` 和 submit 的一样走「等帧 → 读 DONE」那条路。
        """
        if self._dead:
            raise BackendError("backend exited")
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            q = _Request(rid)
            self._reqs[rid] = q
        header = "RESUME %d %d %d\n" % (rid, session, max_new_tokens)
        with self._write_lock:
            try:
                self.proc.stdin.write(header.encode("ascii"))
                self.proc.stdin.flush()
            except Exception as exc:
                with self._lock:
                    self._reqs.pop(rid, None)
                raise BackendError("failed to write resume: %r" % (exc,))
        q.t_submit = time.monotonic()
        return q

    def count(self, prompt):
        """精确数一段文本的 token 数（改动 A 的窗口预算用）。

        走后端新增的 `COUNT` 帧：不占会话、不进调度队列、不碰设备，后端就地 tokenize
        一次回 `COUNTED <rid> <n>`。数不出来（后端没了 / 超时 / 后端回 -1）返回 None——
        调用方据此退回"不裁剪"，绝不拿一个错的预算去裁对话。
        """
        if self._dead or self.proc is None or self.proc.stdin is None:
            return None
        payload = prompt.encode("utf-8")
        if len(payload) > MAX_COUNT_BYTES:
            # 超大文本不进 tokenizer（见 MAX_COUNT_BYTES 注释），按 3 字节/token 估算。
            return max(1, len(payload) // 3)
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            q = _Request(rid)
            self._reqs[rid] = q
        header = "COUNT %d %d\n" % (rid, len(payload))
        # 与 submit 同一个理由：头行与载荷必须一次持锁写完，否则与别的线程的帧交错。
        with self._write_lock:
            try:
                self.proc.stdin.write(header.encode("ascii") + payload)
                self.proc.stdin.flush()
            except Exception:                          # noqa: BLE001
                with self._lock:
                    self._reqs.pop(rid, None)
                return None
        try:
            if not q.done.wait(COUNT_TIMEOUT):
                return None
            n = q.counted_tokens
            return n if (n is not None and n >= 0) else None
        finally:
            self.release_request(q)

    def _kv_op(self, op, session, path, ctx_tokens=0):
        """改动 C 的 KV 存/取底层：发一帧 SAVE/LOAD + 路径，等 SAVED/LOADED。

        失败一律返回 False（后端没了 / 超时 / 后端回 0）。调用方按"这次换页没成功"
        处理——最坏退回换页之前的行为（重新 prefill），不会更糟。
        """
        if self._dead or self.proc is None or self.proc.stdin is None:
            return False
        payload = path.encode("utf-8")
        with self._lock:
            rid = self._next_rid
            self._next_rid += 1
            q = _Request(rid)
            self._reqs[rid] = q
        if op == "LOAD":
            header = "LOAD %d %d %d %d\n" % (rid, session, ctx_tokens, len(payload))
        else:
            header = "SAVE %d %d %d\n" % (rid, session, len(payload))
        with self._write_lock:
            try:
                self.proc.stdin.write(header.encode("ascii") + payload)
                self.proc.stdin.flush()
            except Exception:                          # noqa: BLE001
                with self._lock:
                    self._reqs.pop(rid, None)
                return False
        try:
            if not q.done.wait(KV_OP_TIMEOUT):
                return False
            return bool(q.op_ok)
        finally:
            self.release_request(q)

    def save_kvcache(self, session, path):
        return self._kv_op("SAVE", session, path)

    def load_kvcache(self, session, path, ctx_tokens=0):
        return self._kv_op("LOAD", session, path, ctx_tokens)

    def wait(self, q, timeout=REQUEST_TIMEOUT):
        if not q.done.wait(timeout):
            raise BackendError("timeout after %.0fs waiting for request %d" % (timeout, q.rid))
        if q.error:
            raise BackendError(q.error, soft=q.rejected)
        return q

    def release_request(self, q):
        with self._lock:
            self._reqs.pop(q.rid, None)


# ===========================================================================
# 2. 会话池（粘性 + 前缀校验）
# ===========================================================================

_VERBOSE = False        # 由 main() 按 --verbose 置位。模块级，因为会话池拿不到 Gateway

# ---- 改动 B：削冗余 prompt（保守：工具块稳定化 + 超长工具结果截断）----------------
# 工具定义被渲染进 system（Qwen3.5 模板），工具顺序一变 system 字节就变，粘性前缀判据
# 失效、整轮全量重算（答案对、只是静默变慢）。按 function.name 排序即消除这个漂移。
_TOOL_STABLE = True
# 单条 tool 结果（文件内容/命令输出）的字节上限，0 = 不截断。工具结果是上下文膨胀的
# 大头，而 Claude 的 system+工具定义已占去不少，留点余量给真正的对话。默认 8 KB。
_TOOL_OUTPUT_MAX_BYTES = 8192


def normalize_tools(tools):
    """把工具定义排成稳定顺序（按 function.name）。

    客户端每轮传同一份 tools，但顺序可能变（某些 SDK 遍历 dict/set 的顺序不稳定）。
    渲染进 system 的字节一变，粘性前缀判据就判"对不上"、整轮全量重算。按名字排序后，
    同一工具集无论客户端什么顺序，渲出的字节都一样。参数为空的条目原样保留（排序稳定）。
    """
    def key(t):
        fn = t.get("function") if isinstance(t, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else None
        return str(name) if name else ""
    return sorted([t for t in (tools or []) if isinstance(t, dict)], key=key)


def truncate_tool_result(text, max_bytes=None):
    """超长工具结果按 head+tail 截断（改动 B，保守）。

    直接丢尾部会丢掉关键报错/结论，所以两头都留：head 70% + tail 30%，中间插一行说明
    省略了多少字节。截断必须**确定性**（同一输入同一输出），否则粘性前缀会因截断结果
    漂移而失效。切点落在多字节字符中间时按字节忽略半个字（errors="ignore"）。
    """
    cap = _TOOL_OUTPUT_MAX_BYTES if max_bytes is None else max_bytes
    if not cap or cap <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    head_b = raw[:int(cap * 0.7)]
    tail_b = raw[len(raw) - (cap - int(cap * 0.7)):]
    head_s = head_b.decode("utf-8", "ignore")
    tail_s = tail_b.decode("utf-8", "ignore")
    omitted = len(raw) - len(head_s.encode("utf-8")) - len(tail_s.encode("utf-8"))
    return head_s + ("\n...[truncated %d bytes]...\n" % omitted) + tail_s


def _note(fmt, *args):
    """会话池的事件（排队、回收）。

    为什么要单独一条日志：**多用户场景下"谁在等、谁被回收了"是唯一能看见调度的地方**。
    只盯着 tok/s 看不出排队——第 6 个人没拿到会话时他不会变慢，他是拿不到。
    """
    if _VERBOSE:
        sys.stderr.write("[pool] " + (fmt % args) + "\n")
        sys.stderr.flush()


def _key_label(key):
    """把会话身份缩短，好进日志。显式的 id 原样保留（`id:alice`），内容摘要截断。"""
    if key is None:
        return "(anon)"
    return key if len(key) <= 24 else key[:21] + "..."


# ③ 请求头信号解析（可选，非法一律回默认，绝不因头写错而 500）
def _hdr_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _hdr_float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hdr_deadline(v):
    """`X-Deadline-Ms`：绝对 epoch 毫秒。空/非法/非正 -> None（EDF 退回默认 TTFT 目标）。"""
    try:
        ms = float(v)
        return ms / 1000.0 if ms > 0 else None
    except (TypeError, ValueError):
        return None


class Lease(object):
    __slots__ = ("session", "sent_prompt", "base", "reset", "key", "waited", "swapped")

    def __init__(self, session, sent_prompt, base, reset, key=None, waited=0.0, swapped=False):
        self.session = session
        self.sent_prompt = sent_prompt
        self.base = base            # acquire 时该会话已知的文本（reset 时为 ""）
        self.reset = reset
        self.key = key              # 这段对话的身份，release 时要靠它续期
        self.waited = waited        # 排队等了多久（0 = 没等）
        self.swapped = swapped      # 本轮是否从 /dev/shm **冷读回**（改动 C）；供 X-KV-Reuse 回报


class SessionPool(object):
    """把对话钉到会话上，并决定每轮能不能只发差异部分。

    这里的 `known[s]` 是"该会话的 KV 里确定已经装进去的文本"，三态：
      · 字符串 => 确定装着这些文本，能不能复用只看一条判据：本次完整 prompt 是否以它
        开头。判据不成立就 RESET 发全量——**正确性靠这条判据，不靠粘性记录**，所以
        映射错了最多慢一点，不会答出串味的上下文。
      · ""     => 确定是空的（刚启动、或上一轮自己在空会话上跑完）。
      · None   => 内容未知（清过 KV，而清完之后又被写进了什么，我们从帧序上分不出来）。
        必须 RESET。这一态是踩过坑才加的：见 release() 的注释。
    """

    def __init__(self, backend, idle_ttl=DEFAULT_IDLE_TTL,
                 queue_timeout=DEFAULT_QUEUE_TIMEOUT,
                 contend_idle=DEFAULT_CONTEND_IDLE,
                 kv_swap_dir=None, kv_swap_cap=DEFAULT_KV_SWAP_CAP,
                 kv_swap_max_mb=DEFAULT_KV_SWAP_MAX_MB,
                 sched_policy="fcfs", sched_weights=None,
                 sched_long_tokens=None, sched_long_max=None,
                 sched_cost="full"):
        self.backend = backend
        self.n = backend.sessions
        self.cv = threading.Condition()
        self.known = [""] * self.n
        self.busy = [False] * self.n
        self.dead = [False] * self.n
        self.bound = {}          # conversation key -> session index
        self.last_used = {}      # conversation key -> 最后一次提问的 monotonic 时刻
        self.waiting = {}        # conversation key -> 进队列的 monotonic 时刻（仅用于观测）
        # 空闲回收：一段对话静默超过 idle_ttl 就把它占的会话收回给排队的人。
        # **这不是优化，是"排队"能成立的前提**：没有它，5 个会话被 5 段对话绑着时第 6 段
        # 对话永远等不到——网关根本不知道谁"说完了"（客户端不发结束消息，浏览器关了也
        # 没人通知）。0 或负数 = 不回收（那就等价于第 6 段对话永远排队）。
        self.idle_ttl = float(idle_ttl or 0)
        # 第二级：**已经有请求非等不可**（一个空闲会话都没有）时用的静默阈值。见文件头
        # DEFAULT_CONTEND_IDLE 的注释。0 = 关掉第二级。
        self.contend_idle = float(contend_idle or 0)
        self.queue_timeout = float(queue_timeout)
        self.reaped = 0          # 累计回收了几次（观测用）
        # 除 MLFQ 外的优先级策略所需的额外观测量（见 mlfq_sched.pick）：
        #   served    已完成轮数（LAS/Stride 的"已获服务"）
        #   ttft_target_s  TTFT 目标（EDF 的截止期 = 入队时刻 + 它）
        self.served = {}
        self.ttft_target_s = float(os.environ.get("SCHED_TTFT_TARGET_S", "30") or 30)
        # bound 的上限：键是"system + 首条 user"的摘要或被回收之前攒下的对话。有了空闲
        # 回收，正常情况这张表只装活跃对话；这个上限只是长期运行的安全网——丢绑定只影响
        # 速度，不影响正确性。
        self.bound_cap = 4096
        # 改动 C：会话换页。kv_swap_dir 为 None = 关闭（默认，行为与之前完全一致）。
        # swapped[key] = {"path": 文件基路径, "known": 存取时该会话 KV 对应的文本}。
        # 回收/让出时把 KV 存盘，同 key 再回来且拿到空闲会话时 load 回来，免去重新 prefill。
        # 旧后端不认识 SAVE/LOAD 帧：设了目录也会卡满 KV_OP_TIMEOUT，所以直接忽略并提示。
        if kv_swap_dir and not getattr(backend, "protocol_v2", False):
            _note("后端不支持 SAVE/LOAD 帧（READY 只有两字段），已忽略 --kv-swap-dir；"
                  "需要先重新交叉编译后端（scripts/build_serve.sh）")
            kv_swap_dir = None
        self.kv_swap_dir = kv_swap_dir
        self.kv_swap_cap = kv_swap_cap
        self.kv_swap_max_bytes = int(kv_swap_max_mb) * 1024 * 1024
        self.swapped = {}        # key -> 记录；dict 保序，超 cap/配额时淘汰最老的
        self.swap_bytes = 0      # 盘上换页文件总字节（按记录 size 累加）
        self.swap_in_total = 0
        self.swap_out_total = 0
        # E1a：最近一次换页的耗时与带宽（供 /v1/pool 观测；未发生为 None）。
        self.last_swap_out_ms = None
        self.last_swap_out_mb_s = None
        self.last_swap_in_ms = None
        self.last_swap_in_mb_s = None
        backend.on_clear = self._on_clear
        # ---- 调度策略（2026-09-29 加）----
        # `fcfs`（默认）= 与改造前**逐字节一致**的行为：会话空出来时，等待者谁先被条件
        # 变量叫醒谁拿（"先到先得、不保证严格顺序"）。`mlfq` = 会话空出来时，先对队列里
        # 的候选打分（五因子，全部可观测近似），只让得分最高的那个拿走——见 mlfq_sched.py。
        # 这一层**只决定"派给谁"**：不改变任何请求语义，也不动 KV 记账（known[]/bound[]）。
        self.sched_policy = (sched_policy or "fcfs").lower()
        # 成本口径（2026-10 加）：cp 用"整段 prompt"还是"增量"。delta = 整段×(1−pf)，
        # 即复用后真正要 prefill 的部分——多轮 Agent 下更贴合真实成本。
        self.sched_cost = (sched_cost or "full").lower()
        self.sched_weights = (mlfq_sched.parse_weights(sched_weights)
                              if sched_weights is not None else mlfq_sched.DEFAULT_WEIGHTS)
        self.sched_long_tokens = (float(sched_long_tokens) if sched_long_tokens
                                  else float(mlfq_sched.DEFAULT_LONG_TOKENS))
        self.sched_long_max = (int(sched_long_max) if sched_long_max
                               else max(1, self.n // 2))
        self.wait_req = {}       # key -> {"head","est","max_new"}：MLFQ 打分的输入
        self.long_busy = set()   # 正在跑长任务的会话号（长任务限流用）

    def _on_clear(self, session, ctx, limit):
        # 后端把该会话的 KV 清了（RESET 或上下文将满）。粘性记录必须立刻作废，否则下一轮
        # 网关以为历史还在、只发差异部分，而模型那边什么都没有——答出来的东西看着通顺
        # 却串味，是最难查的一类错。
        #
        # 置 None（= 未知，下一轮必须 RESET）而不是 ""（= 确定是空的）：清 KV 只是这段
        # 历史的**起点**，紧接着这次请求自己的 prompt 就会被 prefill 进去。此刻这次
        # 请求的 release() 还没跑（它在 HTTP 线程里等 DONE），所以这个窗口里"KV 是空的"
        # 这句话是错的；等 release() 回来会用「本轮发出 + 本轮生成」写回确定值。
        with self.cv:
            if 0 <= session < self.n:
                self.known[session] = None
            self.cv.notify_all()

    def mark_dead(self, session):
        # 会话的推理失败后，demo 里那个驱动线程会退出，钉在它上面的请求永远不会被消费
        # （会一直躺在待办队列里占额度）。这里把它标死，不再往上派活。
        with self.cv:
            if 0 <= session < self.n:
                self.dead[session] = True
                self.busy[session] = False
                self.long_busy.discard(session)
            for key, s in list(self.bound.items()):
                if s == session:
                    del self.bound[key]
                    self.last_used.pop(key, None)
                    self.wait_req.pop(key, None)
                    rec = self.swapped.pop(key, None)
                    if rec is not None:
                        self.swap_bytes -= rec.get("size", 0)
                        self._unlink_swap(rec["path"])
            self.cv.notify_all()

    # ---- 改动 C：会话换页（KV 存盘 / 读回）------------------------------------

    def _swap_path(self, key):
        h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.kv_swap_dir, "conv_%s.kv" % h)

    def _unlink_swap(self, base):
        """删掉一段换页 KV 的所有分段文件（后端写的是 base_seg0.kv、base_seg1.kv…）。"""
        stem = base[:-3] if base.endswith(".kv") else base
        for p in glob.glob(stem + "*"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _swap_out(self, key, session):
        """把会话 KV 存盘（调用方须已持 self.cv，且该会话不 busy）。

        代价说明：这会**持着池锁**做一次 GB 级的 PCIe 存取（秒级），期间别的 HTTP 线程
        拿不到锁。它只发生在 idle 回收/让出的时刻（会话本来就没在跑），换页开启时接受
        这点停顿；关闭换页（默认）则完全不发生。
        """
        if not self.kv_swap_dir:
            return False
        known = self.known[session]
        if not known:               # 空 KV / 内容未知：没有可存的价值
            return False
        # 落盘前先看目标盘余量：kv_swap_dir 常是 /dev/shm（内存），塞满会波及整机。
        try:
            free = shutil.disk_usage(self.kv_swap_dir).free
        except OSError:
            free = 0
        if free < KV_SWAP_MIN_FREE_BYTES:
            _note("换页跳过（%s 余量 %.0f MB 不足）：%s s%d"
                  % (self.kv_swap_dir, free / 1e6, _key_label(key), session))
            return False
        path = self._swap_path(key)
        _t0 = time.time()
        if not self.backend.save_kvcache(session, path):
            _note("换页失败（存）：%s s%d -> %s" % (_key_label(key), session, path))
            return False
        _out_ms = (time.time() - _t0) * 1e3
        old = self.swapped.pop(key, None)
        if old is not None:
            self.swap_bytes -= old.get("size", 0)
            self._unlink_swap(old["path"])
        size = self._swap_bytes_on_disk(path)
        _out_mb = size / 1e6
        self.last_swap_out_ms = round(_out_ms, 1)
        self.last_swap_out_mb_s = round(_out_mb / (_out_ms / 1e3), 1) if _out_ms > 0 else None
        _note("[swap] out %s s%d %.1fMB %.0fms => %.0fMB/s"
              % (_key_label(key), session, _out_mb, _out_ms, self.last_swap_out_mb_s or 0))
        self.swapped[key] = {"path": path, "known": known, "size": size}
        self.swap_bytes += size
        self.swap_out_total += 1
        # cap（条数）与配额（字节）任一超了，都按 LRU 删最老的——绝不能把 /dev/shm 写满。
        while (len(self.swapped) > self.kv_swap_cap
               or self.swap_bytes > self.kv_swap_max_bytes):
            if len(self.swapped) <= 1:
                break                   # 只剩当前这条：再删就把刚存的也删了，无意义
            old_key = next(iter(self.swapped))
            evicted = self.swapped.pop(old_key)
            self.swap_bytes -= evicted.get("size", 0)
            self._unlink_swap(evicted["path"])
        _note("换页：%s s%d -> %s（盘上 %d 段 / %.0f MB）"
              % (_key_label(key), session, path, len(self.swapped),
                 self.swap_bytes / 1e6))
        return True

    def _swap_bytes_on_disk(self, base):
        """一段换页 KV 的所有分段文件字节数之和（后端写 base_seg0.kv、base_seg1.kv…）。"""
        stem = base[:-3] if base.endswith(".kv") else base
        total = 0
        for p in glob.glob(stem + "*"):
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def _swap_in(self, session, key):
        """把某段对话的 KV 读回一个空闲会话（调用方须已持 self.cv）。

        load 成功才把 known 设成记录里的文本，`_make_lease` 于是能按前缀发增量；load 失败
        就删记录、按空会话走（最坏全量重算，不会更糟）。token 数用后端 COUNT 现算——
        known 就是那段 KV 对应的文本，数它即可。
        """
        rec = self.swapped.pop(key, None)
        if rec is None or not self.kv_swap_dir:
            return False
        self.swap_bytes -= rec.get("size", 0)
        n = self.backend.count(rec["known"])
        _t0 = time.time()
        if not self.backend.load_kvcache(session, rec["path"], n or 0):
            _note("换页失败（读）：%s -> s%d，退回全量重算" % (_key_label(key), session))
            self._unlink_swap(rec["path"])
            return False
        _in_ms = (time.time() - _t0) * 1e3
        _in_mb = rec.get("size", 0) / 1e6
        self.last_swap_in_ms = round(_in_ms, 1)
        self.last_swap_in_mb_s = round(_in_mb / (_in_ms / 1e3), 1) if _in_ms > 0 else None
        _note("[swap] in %s s%d %.1fMB %.0fms => %.0fMB/s"
              % (_key_label(key), session, _in_mb, _in_ms, self.last_swap_in_mb_s or 0))
        # 已经读回内存（KV 重新驻留 NPU），盘上这份就没用了，删掉腾空间。
        self._unlink_swap(rec["path"])
        self.known[session] = rec["known"]
        self.swap_in_total += 1
        _note("换页：%s -> s%d 读回（%d tok）" % (_key_label(key), session, n or 0))
        return True

    def _reap_idle_locked(self, now, ttl=None, limit=None, why="静默"):
        """把静默超过 TTL 的对话占的会话收回。调用方必须已持 self.cv。

        只收**没在跑**的会话；正在跑的那一轮不能被打断（打断也没意义，它马上就还回来）。
        回收只解绑 + 作废 known，**不动 KV**——下一个拿到这个会话的对话若前缀对不上，
        `_make_lease` 自己会 RESET。所以回收本身不会有额外的清 KV 开销。

        `ttl=None` 用常规的 `idle_ttl`（没人排队时的行为）；给 `contend_idle` 就是
        "有人在等"那一级。`limit` 限制收几个——**抢手的那一级只收一个**，收多了等于把
        好几段对话的 KV 一起扔掉，只为了给一个请求腾地方。

        一轮里按**静默时长从长到短**收，不是按 dict 顺序：受害者必须是"最闲的那一段"，
        否则同一份脚本会有时抢到 A、有时抢到 B，行为不可预期（这正是当初决定"不抢占"
        的理由，现在用"按静默挑"把它解决掉）。
        """
        ttl = self.idle_ttl if ttl is None else ttl
        if ttl <= 0:
            return 0
        victims = sorted(((now - self.last_used.get(k, now), k, s)
                          for k, s in self.bound.items() if not self.busy[s]),
                         reverse=True)
        n = 0
        for idle, k, s in victims:
            if idle <= ttl:
                break                       # 已按静默倒序排好，后面只会更不闲
            if limit is not None and n >= limit:
                break
            # 改动 C：回收前先把这段对话的 KV 存盘（换页开启时）。存失败不影响回收——
            # 只是下次回来要重新 prefill，和没有换页时一样。
            if 0 <= s < self.n:
                self._swap_out(k, s)
            del self.bound[k]
            self.last_used.pop(k, None)
            self.wait_req.pop(k, None)
            self.long_busy.discard(s)
            if 0 <= s < self.n:
                self.known[s] = None        # 里面的内容从此未知，下一轮必须 RESET
            self.reaped += 1
            n += 1
            _note("会话回收：%s %s %.1fs > %.1fs，s%d 交还给排队者"
                  % (_key_label(k), why, idle, ttl, s))
        return n

    def _dequeue(self, key):
        """把一段对话移出等待队列：`waiting`（观测）与 `wait_req`（MLFQ 打分输入）一起清。

        两本账必须同步——只清一本的话，MLFQ 会给一个早就不在等的请求打分，
        或者反过来漏掉一个还在等的请求。
        """
        self.waiting.pop(key, None)
        self.wait_req.pop(key, None)

    def _sched_pick(self, best, now):
        """按 self.sched_policy 选一个候选（策略表见 mlfq_sched.pick）。

        候选排除**有主、正在等自己会话**的对话（见 acquire 里 `cand is not None` 分支）。
        统一信号：cp(估算算力) cr sa(等待) pf(前缀) tl(工具空闲) svc(已获服务) dl(截止期 slack)。
        """
        cands = []
        for k, t in list(self.waiting.items()):
            rec = self.wait_req.get(k)
            if rec is None:
                continue
            b = self.bound.get(k)
            if b is not None and not self.dead[b]:
                continue
            pf = mlfq_sched.prefix_ratio(rec["head"], self.known[best])
            est = rec["est"]
            # 成本口径：full=整段；delta=复用后真正要 prefill 的增量（+ 本轮 max_new）。
            # 复用来源有二：① 与候选会话前缀重合 pf；② **该对话自身的 KV 还在**（`k in swapped`
            # => 冷读回，prefill≈增量）。只按 pf 会漏掉②（调度时 best 多为干净会话）。
            reuse = pf
            if k in self.swapped:
                reuse = 1.0
            cp = (est * (1.0 - reuse)) if self.sched_cost == "delta" else est
            cands.append({
                "key": k,
                "cp": cp + rec["max_new"],                   # 本轮估算算力
                "cr": 0.0,                                   # 端侧不可观测（见模块注释）
                "sa": max(0.0, now - t),                     # 饥饿避免：等待时长
                "pf": pf,
                "tl": mlfq_sched.tool_latency_proxy(now, self.last_used.get(k)),
                "svc": float(self.served.get(k, 0)),          # 已完成轮数（LAS/Stride）
                "dl": ((rec.get("deadline") or (t + self.ttft_target_s)) - now),  # 截止期 slack（EDF）
                "cls": rec.get("cls", 0),                     # 优先级类（prio）
                "wq": rec.get("wq", 1.0),                     # 权重（stride）
            })
        return mlfq_sched.pick(self.sched_policy, cands, self.sched_weights, now,
                               max_inflight_long=self.sched_long_max,
                               long_tokens=self.sched_long_tokens,
                               long_inflight=len(self.long_busy))

    def acquire(self, key, prompt, timeout=None, max_new=None,
                cls=0, wq=1.0, deadline=None):
        """返回 Lease；排队超过 timeout 仍拿不到就抛 BackendError。

        **不抢占**：所有会话都被别的对话绑着时，这里只等，不把谁的会话夺过来。
        抢夺看着"响应快"，代价却是被抢的那段对话下一轮前缀对不上、要全量重算 prefill
        （多花 2~3 秒），而且**谁被抢取决于请求到达顺序**——同一份脚本时好时坏。
        等，加上 `_reap_idle_locked` 把真正闲下来的会话交出来，行为才是可预期的。
        """
        if timeout is None:
            timeout = self.queue_timeout
        deadline = time.time() + timeout
        t_enter = time.time()
        queued = False
        with self.cv:
            while True:
                # 等不出结果的两件事，必须在**进队列之前**判掉。不判的代价不是"慢一点"，
                # 是请求在队列里空等满 queue_timeout 而屏幕上什么都不打印——现场看起来
                # 像"网关卡了"，真相在 `GET /v1/pool` 里一眼可见（四个 slot 全是 dead）。
                #   · 后端子进程没了：一个超大请求就能让它退出（见 main.cc 的
                #     kServeMaxPromptBytes 分支），池子里再等也不会凭空多出会话来；
                #   · 四个会话全 dead：dead 没有任何复活路径（见 mark_dead），等下去
                #     是纯粹的干等。
                # 2026-09-17 上板实测：一个 3.4 MB 的请求打死后端 → 四个会话全 dead →
                # 5 段对话（4 个终端 + 1 个 Claude Code）在这里排到 600s 超时才收到 503。
                if not self.backend.alive():
                    self._dequeue(key)
                    raise BackendError("backend exited; every request fails until the "
                                       "gateway is restarted")
                if all(self.dead):
                    self._dequeue(key)
                    raise BackendError("all %d sessions are dead; restart the gateway"
                                       % self.n)

                # 优先复用已经绑在这一段对话上的会话：只有它才可能已经有这段历史。
                cand = self.bound.get(key)
                if cand is not None and not self.dead[cand] and not self.busy[cand]:
                    if queued:
                        self._dequeue(key)
                        _note("排队结束：%s 等了 %.1fs，拿到 s%d"
                                              % (_key_label(key), time.time() - t_enter, cand))
                    return self._make_lease(cand, prompt, t_enter, key)
                if cand is not None and not self.dead[cand]:
                    pass        # 同一段对话的上一轮还在跑：等它，不能换会话
                else:
                    # 挑一个空闲会话。**"空闲"不等于"没主"**：会话可能正被另一段对话绑着
                    # （bound 里挂着），只是它此刻没在跑。把新对话派到那种会话上，两段对话
                    # 就会钉在同一个会话上——后端同一个会话一次只跑一条，于是它们只能互相
                    # 等，答案照样对、吞吐掉一半。所以候选里必须排除有主的。
                    now = time.time()
                    self._reap_idle_locked(now)
                    owned = set(self.bound.values())
                    free = [s for s in range(self.n)
                            if not self.dead[s] and not self.busy[s] and s not in owned]
                    # `idle_ttl <= 0` 是**总开关**（文档里的"0 = 不回收"）：第二级只是
                    # 对它的细化，不能绕过它。回归套件正是靠 `--idle-ttl 0` 来证明自己
                    # 真的在主动 close、而不是碰巧被回收救了——这里绕过就等于把那套证明
                    # 悄悄作废。
                    if not free and self.contend_idle > 0 and self.idle_ttl > 0:
                        # 一个空闲会话都没有 = 这个请求**非等不可**，才启用第二级阈值。
                        # 放在"确认没得用"之后判，是为了让常规路径（还有空闲会话）与
                        # 之前逐字节一样——那才是 KV 复用真正省下时间的地方。
                        # 只收一个：腾出够它用的地方就行，别顺手把好几段对话的 KV 扔掉。
                        self._reap_idle_locked(now, ttl=self.contend_idle, limit=1,
                                               why="静默（有人排队）")
                        owned = set(self.bound.values())
                        free = [s for s in range(self.n)
                                if not self.dead[s] and not self.busy[s] and s not in owned]
                    if free:
                        # 优先没装过东西的（省一次 RESET）：unknown（None）排在有内容的前面，
                        # 反正都要 RESET，清空的会话至少不用先扔垃圾。
                        best = min(free, key=lambda s: (self.known[s] is not None, s))
                        # ---- MLFQ：会话空出来时，队列里不止一个人等就先打分选一个 ----
                        # 默认 fcfs 不进这里（行为与改造前逐字节一致，见 mlfq_sched 注释）。
                        # `winner is None` = 长任务已到并发上限、而候选全是长任务：谁都别派。
                        take = True
                        if self.sched_policy != "fcfs" and len(self.waiting) > 1:
                            winner, _why = self._sched_pick(best, now)
                            take = (winner == key)
                        if take:
                            # 改动 C：这段对话之前被换出过，且拿到的会话是干净的 => 读回 KV，
                            # 让 _make_lease 能按前缀只发增量（免一次全量 prefill）。
                            did_swap = False
                            if (key is not None and key in self.swapped
                                    and not self.known[best]):
                                did_swap = self._swap_in(best, key)
                            # 长任务限流记账：本轮估算 prompt 超阈值就占一个名额，跑完释放。
                            req = self.wait_req.get(key) or {}
                            if (req.get("est", 0.0) + req.get("max_new", 0)
                                    > self.sched_long_tokens):
                                self.long_busy.add(best)
                            if queued:
                                self._dequeue(key)
                                _note("排队结束：%s 等了 %.1fs，拿到 s%d"
                                                      % (_key_label(key), time.time() - t_enter, best))
                            # key 是 None（请求里既没有 conversation_id 也没有 user，连一条
                            # user 消息都没有 => conversation_key 返回 None）= **无身份的请求，
                            # 不能记绑定**。记了的话所有匿名请求都会共用 bound[None] 这一个
                            # 条目，于是它们全被钉到同一个会话上互相等——两段素不相识的对话
                            # 排成一队，正是这个池子存在的意义反面。
                            if key is not None:
                                self.bound[key] = best
                                self.last_used[key] = now
                                if len(self.bound) > self.bound_cap:
                                    # 安全网，正常有回收就走不到。丢绑定只让那段对话下次重新
                                    # 挑会话，正确性不受影响；不丢就是一条随对话数线性增长的
                                    # 慢泄漏。
                                    oldest = next(iter(self.bound))
                                    self.bound.pop(oldest)
                                    self.last_used.pop(oldest, None)
                            _lease = self._make_lease(best, prompt, t_enter, key)
                            _lease.swapped = bool(did_swap)   # 本轮是否从 /dev/shm 冷读回
                            return _lease
                        # 不是最高分（或全员被长任务限流）：不派发，落到下面的
                        # deadline 判断与排队等待，等下一次机会。这里**不能**用 continue——
                        # 那会跳过 deadline，让排队超时失效。
                if time.time() >= deadline:
                    self._dequeue(key)
                    # 能排满 timeout 才走到这里，说明不是"别人占着没放"，而是**四个会话
                    # 全都在真的跑**（第二级回收只收没在跑的，所以它救不了这种情况）。
                    # 这条区分很重要：用户问"NPU 没人用为什么还要等"时，看一眼这个提示
                    # 和 `GET /v1/pool` 就能知道是排队还是真忙。
                    raise BackendError(
                        "no session available: all %d sessions are busy or bound to "
                        "other conversations (waited %.0fs; idle_ttl=%.0fs; "
                        "contend_idle=%.0fs). Retry later or reuse a conversation_id "
                        "that already has a session."
                        % (self.n, timeout, self.idle_ttl, self.contend_idle))
                if not queued:
                    queued = True
                    self.waiting[key] = t_enter
                    # MLFQ 打分的三个输入。head 截断到 128 KB：足够判断"是否以 known 为前缀"
                    # （pf 因子），又不会把整份大 prompt 一直留在池子里。
                    self.wait_req[key] = {
                        "head": prompt[:131072],
                        "est": mlfq_sched.estimate_prompt_tokens(prompt),
                        "max_new": int(max_new or 0),
                        # ③ 请求头信号（可选）：优先级类 / 权重 / 截止期（epoch 秒）。
                        "cls": int(cls or 0),
                        "wq": float(wq or 1.0),
                        "deadline": float(deadline) if deadline else None,
                    }
                    _note("进入排队：%s 等空闲会话（当前 %d/%d 被占，"
                                          "队列 %d 人）"
                                          % (_key_label(key), len(self.bound), self.n,
                                             len(self.waiting)))
                # 唤醒间隔取小值：等待期间会有别人 release / 到点该回收，靠它重查。
                self.cv.wait(min(0.5, max(0.05, deadline - time.time())))

    def close(self, key):
        """客户端主动说"这段对话结束了"：立刻把会话交还给池子。

        为什么必须有这个口子：网关**不知道一段对话什么时候结束**（客户端不发关闭消息），
        所以只能靠 `IDLE_TTL` 超时回收。对"来问一句就走"的客户端（脚本、Agent）这很糟：
        他一个人连着问 4 个不相干的问题，就占满 4 个会话、把后面所有人挡在队列里
        **整整一个 IDLE_TTL**。排队策略要想在真实使用里成立，就得有一条"我走了"的路。

        返回放掉的 session 号；`None` = 这段对话本来就没有会话（幂等，重复 close 不报错）。
        `busy` 时抛 `BackendError`（这一轮还在跑，不能抽走它脚下的会话；等它答完再来）。
        """
        if key is None:
            raise BackendError("no conversation id given")
        with self.cv:
            s = self.bound.get(key)
            if s is None:
                return None
            if self.busy[s]:
                raise BackendError("conversation is still generating; retry after it "
                                   "finishes")
            del self.bound[key]
            self.last_used.pop(key, None)
            self.wait_req.pop(key, None)
            self.long_busy.discard(s)
            # 改动 C：这段对话结束了，盘上的换页 KV 也没用了，删掉省空间。
            rec = self.swapped.pop(key, None)
            if rec is not None:
                self.swap_bytes -= rec.get("size", 0)
                self._unlink_swap(rec["path"])
            if 0 <= s < self.n:
                # 内容从此未知：下一个拿到它的人必须 RESET，否则会把新 prompt 追加在
                # 上一段对话的 KV 后面（= 坑 1 那个"两份历史"）。**不能写 ""**。
                self.known[s] = None
            self.cv.notify_all()
            _note("会话交还：%s 放掉 s%d" % (_key_label(key), s))
            return s

    def acquire_continue(self, key):
        """「继续回答」用的租约：拿回**同一段对话**占的会话，不 RESET、不发新 prompt。

        和 acquire 的关键差别：不传 prompt、不做前缀匹配——RESUME 根本不 prefill，
        KV 里已经装着上一轮 STOP 时的状态（prompt + 已生成的 token）。所以这个租约的
        `sent_prompt` 恒为空串、`reset` 恒为 False，`base` 是 known[s]（release 记账
        时要拿它拼出新的 known）。

        找不到会话（这段对话已经被 idle 回收，或根本没这号）就报错：继续一个不存在的
        上下文没有意义，客户端拿到明确的 400 比静默跑一条空 RESUME 好查得多。
        """
        if key is None:
            raise BackendError("no conversation id to continue")
        t_enter = time.time()
        with self.cv:
            s = self.bound.get(key)
            if s is None:
                raise BackendError("this conversation has no session to continue "
                                   "(it may have been reaped by idle timeout)")
            deadline = t_enter + self.queue_timeout
            while self.busy[s]:
                if time.time() >= deadline:
                    raise BackendError("session is still generating; cannot continue")
                self.cv.wait(min(0.5, max(0.05, deadline - time.time())))
            waited = max(0.0, time.time() - t_enter)
            self.busy[s] = True
            self.last_used[key] = time.time()
            base = self.known[s] if self.known[s] else ""
            return Lease(s, "", base, False, key=key, waited=waited)

    def snapshot(self):
        """给 `GET /v1/pool` 看的一眼状态：谁占着哪个会话、谁在等。

        为什么需要它：多用户场景下"第 6 个人在排队"这件事**从吞吐上是看不出来的**
        ——他没拿到会话，不是变慢了。终端用户和排障的人都得有个地方能看。
        """
        now = time.time()
        with self.cv:
            by_sess = {}
            for k, s in self.bound.items():
                by_sess[s] = k
            slots = []
            for s in range(self.n):
                k = by_sess.get(s)
                slots.append({
                    "session": s,
                    "state": ("dead" if self.dead[s] else
                              "busy" if self.busy[s] else
                              "idle" if k is not None else "free"),
                    # null = 没人占（不要写成字符串 "(anon)"，那看起来像"有个叫 anon 的
                    # 用户在占着"，正交性会丢）
                    "conversation": None if k is None else _key_label(k),
                    # 原始键。`conversation` 是给人看的（长了会截断），这个是给机器用的：
                    # 匿名对话（`h:...`）的键客户端算不出来，只能从这儿读出来，再用
                    # `POST /v1/conversations/close {"key": ...}` 把它放掉。没有这一条，
                    # 一段匿名对话会一直占到 IDLE_TTL 超时——**看得到却踢不掉**。
                    "key": k,
                    "idle_s": (round(now - self.last_used.get(k, now), 1)
                               if k is not None and not self.busy[s] else None),
                })
            waiting = [{"conversation": _key_label(k), "waited_s": round(now - t, 1)}
                       for k, t in sorted(self.waiting.items(), key=lambda kv: kv[1])]
            return {
                "sessions": self.n,
                "idle_ttl_s": self.idle_ttl,
                # 第二级阈值也报出来：只看 `/v1/pool` 的人得能算出"我这个请求最坏等几秒"。
                # 有它，`排队 226s` 这种现场不用再翻日志猜是哪一级没生效。
                "contend_idle_s": self.contend_idle,
                "queue_timeout_s": self.queue_timeout,
                "reaped_total": self.reaped,
                # 改动 C：换页观测（关闭换页时恒为 0/0/0）
                "swapped": len(self.swapped),
                # 冷会话的身份标签（供演示页把"历史对话"标成活跃/冷）
                "swapped_keys": [_key_label(k) for k in self.swapped],
                "swap_bytes": self.swap_bytes,
                "swap_out_total": self.swap_out_total,
                "swap_in_total": self.swap_in_total,
                # E1a：最近一次换页的耗时/带宽（MB/s），未发生为 None。
                "last_swap_out_ms": self.last_swap_out_ms,
                "last_swap_out_mb_s": self.last_swap_out_mb_s,
                "last_swap_in_ms": self.last_swap_in_ms,
                "last_swap_in_mb_s": self.last_swap_in_mb_s,
                "slots": slots,
                "waiting": waiting,
            }

    def _make_lease(self, session, prompt, t_enter=None, key=None):
        self.busy[session] = True
        waited = max(0.0, time.time() - t_enter) if t_enter else 0.0
        base = self.known[session]
        if base and prompt.startswith(base):
            return Lease(session, prompt[len(base):], base, False, key=key, waited=waited)
        # 复用不了，两种情况必须分开：
        #   base 非空但前缀对不上 => 里面确实装着别的对话，必须 RESET，否则这次的全量
        #     prompt 会**追加**在旧上下文后面，模型看到两份历史；
        #   base 是 None（清过 KV，内容未知）=> 同样必须 RESET。这里不能写成
        #     `bool(base)`：None 和 "" 都是假值，会把"未知"当"空"处理，正好踩中
        #     上面那个追加的坑。
        return Lease(session, prompt, "", base is None or bool(base),
                     key=key, waited=waited)

    def discard(self, lease):
        """这一轮**根本没跑**（后端在 prefill 前拒了它）：只把会话还回去，不动 known。

        不能走 release()：它会把「本轮发出的 prompt」算进"KV 里确定装着的内容"，而这一
        轮一个 token 都没 prefill 进去。照 release 记账的话，下一轮网关会以为历史都在、
        只发差异部分——模型看到的是一段没有开头的对话，答出来通顺但串味，正是这套记账
        存在的意义反面。known 这里**一个字节都不动**，两种来源各自已经是对的：
          · 上下文装不下 —— 后端在拒之前先发了 CLEAR，`_on_clear` 已把它置成 None，
            下一轮必定 RESET 全量重发；
          · 载荷超帧上限 —— 后端**不发 CLEAR**（KV 一个字节都没动），known 保持原样
            正好描述着 KV 里真正装着的东西。
        """
        with self.cv:
            s = lease.session
            if 0 <= s < self.n:
                self.busy[s] = False
                self.long_busy.discard(s)
            if lease.key is not None:
                self.wait_req.pop(lease.key, None)
                if lease.key in self.bound:
                    self.last_used[lease.key] = time.time()
                    self.served[lease.key] = self.served.get(lease.key, 0) + 1
            self.cv.notify_all()

    def release(self, lease, generated_text, was_cleared):
        """本轮结束后，把「KV 里现在确定装着什么」写回去。

        这里曾经写成 `if was_cleared: known[s] = ""`——那是**错的**，而且是那种只在
        会话变脏之后才现形的错：Reset 必然伴随一帧 CLEAR，于是每段对话只要第一轮需要
        RESET（会话里装着上一段对话），第二轮开始就永远拿不回粘性，每轮全量重算、
        KV 里还越堆越多份历史。清 KV 发生在**本轮 prefill 之前**（见 main.cc 的
        serve_clear -> clear_conversation_kv -> prefill 顺序），所以本轮结束时 KV 里
        确定装着的就是「本轮发出 + 本轮生成」；被清掉的旧 base 不能再算进去。

        `generated_text` 传进来的是**客户端会回显的版本**（调用方已摘掉 think 段）。
        于是有个已知的小偏差：KV 里其实还留着模型原始生成的 `<think>…</think>`，
        而 known 声称没有。方向是安全的——模型最多多看到一小段空的推理标记（那**本来
        就是它自己这一轮吐的**），不会少看到任何东西；反过来按原始版记账则会让关思考的
        每轮都判"前缀对不上"，缓存静默全废。
        """
        with self.cv:
            s = lease.session
            if 0 <= s < self.n:
                if was_cleared:
                    self.known[s] = lease.sent_prompt + generated_text
                else:
                    self.known[s] = lease.base + lease.sent_prompt + generated_text
                self.busy[s] = False
                self.long_busy.discard(s)
            # 续期：TTL 数的是"这段对话多久没提问了"，所以在这一轮**开始和结束**都刷新。
            # 只刷开始的话，一轮跑很久（长上下文几十秒）会被自己在跑到一半时判成空闲。
            if lease.key is not None:
                self.wait_req.pop(lease.key, None)
                if lease.key in self.bound:
                    self.last_used[lease.key] = time.time()
                    self.served[lease.key] = self.served.get(lease.key, 0) + 1
            self.cv.notify_all()


# ===========================================================================
# 3. 聊天模板渲染
# ===========================================================================

def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):       # OpenAI 允许 content 是分段数组
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                out.append(part.get("text") or "")
        return "".join(out)
    return ""


def render_messages(messages, enable_thinking=None, tools=None):
    """按 Qwen3.5 模板把 OpenAI 的 messages 渲染成一个 prompt 字符串。

    渲染放在网关侧而不是 demo 里：demo 的模板只有「首轮带 system / 后续轮不带」两态，
    表达不了任意角色的历史消息（Agent 会发 system + user + assistant + tool 的长列表）。
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs or msgs[0].get("role") != "system":
        msgs = [{"role": "system", "content": QWEN35_DEFAULT_SYSTEM}] + msgs

    # 关思考：Qwen3 系列用软开关 /no_think，贴成 user 消息末尾的一个词。
    # 实测官方 server 的 chat_template_kw.enable_thinking=false 在这条 GGUF 上不生效
    # （输出照样带 <think>），所以这里退回到模板层面的开关。
    #
    # 贴在**每一条** user 消息上，而不是只贴最后一条。这不是风格选择，是粘性复用的
    # 前提：只贴最后一条的话，第一轮 u1 是"最后一条"（带标记），第二轮 u1 变成历史
    # 消息（不带）=> 同一段历史两轮渲染出的字节不同 => 前缀判据失败 => 每轮全量重算。
    # 实测过这个退化（nothink_check.py）：关思考时第二轮 reset=1、base=0、sent=full，
    # 缓存全废但答案仍然是错的看不出来——属于"静默变慢"。全贴则每轮渲染同一段历史
    # 都是同样的字节，复用照常。第一条 user 在两种规则下都带标记，所以对首轮无影响。
    #
    # **工具返回那个 user 轮不贴**：它是模板合成的、不是用户说的话，贴上去属于往历史里
    # 塞模板没有的东西（渲染结果仍然逐轮稳定，所以复用不受影响）。
    no_think = (enable_thinking is False)

    parts = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        role = m.get("role") or "user"
        text = _content_text(m.get("content"))

        if role == "tool":
            # 模板把工具返回包在 <tool_response> 里，而且**连续多条合并进同一个 user
            # 轮**（一个回合里并行调了三个工具，就是三条 tool 消息）。这跟"每条渲染成
            # 一个 <|im_start|>tool 轮"完全不同——模板里根本没有 tool 角色轮，旧实现
            # 渲成 <|im_start|>tool 是错的，模型收到的是它没见过的形状。
            blocks = []
            while i < len(msgs) and (msgs[i].get("role") or "") == "tool":
                blocks.append(tool_response_block(
                    truncate_tool_result(_content_text(msgs[i].get("content")))))
                i += 1
            parts.append("%suser\n%s%s" % (IM_START, "\n".join(blocks), IM_END))
            continue

        if role == "system" and tools:
            text = render_tools_system(tools, text)
        elif role == "assistant":
            calls = normalize_tool_calls(m.get("tool_calls"))
            if calls:
                text = render_assistant_turn(text, calls)
        if no_think and role == "user":
            text = text + " /no_think"
        if no_think and role == "assistant":
            # 助手轮按模板的"推理包装"渲。关思考时这个包装就是那对空标记，**不含推理
            # 正文**，所以我们渲得起（思考开着时包装里要放模型当时的推理，那是渲染侧
            # 拿不到的东西，见 toolcalls.py 顶部的偏差 1）。
            parts.append("%s%s%s" % (ASSISTANT_HEAD + THINK_OFF, text, IM_END))
        else:
            parts.append("%s%s\n%s%s" % (IM_START, role, text, IM_END))
        i += 1
    # 生成位置同样补上：模型从这里接着写，写出来的就是正文本身。
    parts.append(ASSISTANT_HEAD + (THINK_OFF if no_think else ""))
    return "".join(parts)


def conversation_key(messages, explicit=None):
    """一段对话的身份。

    取"system + 第一条 user 消息"的摘要：它在一段对话的各轮之间稳定，不同对话之间
    基本不会撞。撞了也不会错——前缀校验会挡住复用，只是退回全量重算。
    """
    if explicit:
        return "id:" + str(explicit)
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    sys_text = ""
    first_user = ""
    for m in msgs:
        if m.get("role") == "system" and not sys_text:
            sys_text = _content_text(m.get("content"))
        elif m.get("role") == "user" and not first_user:
            first_user = _content_text(m.get("content"))
        if sys_text and first_user:
            break
    if not first_user:
        return None
    h = hashlib.sha1(("%s\x00%s" % (sys_text, first_user)).encode("utf-8"))
    return "h:" + h.hexdigest()[:16]


def dump_request(req, key, prompt, max_new, tools, stream):
    """把一次请求的摘要写一行 JSONL（调试客户端行为用；默认关，见 DUMP_REQ）。

    写什么：时间、对话身份、是否流式、max_new、渲染后 prompt 字节数、消息条数、
    每条消息的 role/字符数/（system、user 的前 N 字符预览）/tool_calls 名、工具名清单。
    为什么只写摘要：正文可能很大、也可能含敏感内容；摘要足够回答"这一路是主请求还是后台
    辅助调用""本轮调了哪个工具"这类问题。
    """
    if not DUMP_REQ:
        return

    def _chars(x):
        try:
            return len(_content_text(x) or "")
        except Exception:                                       # noqa: BLE001
            return 0

    msgs = []
    for m in (req.get("messages") or []):
        if not isinstance(m, dict):
            continue
        item = {"role": m.get("role"), "chars": _chars(m.get("content"))}
        if m.get("tool_calls"):
            item["tool_calls"] = [((tc.get("function") or {}).get("name")
                                   if isinstance(tc, dict) else None)
                                  for tc in m["tool_calls"]]
        if m.get("tool_call_id"):
            item["tool_call_id"] = m.get("tool_call_id")
        if m.get("role") in ("system", "user"):
            try:
                item["preview"] = (_content_text(m.get("content")) or "")[:DUMP_REQ_CHARS]
            except Exception:                                   # noqa: BLE001
                item["preview"] = ""
        msgs.append(item)
    rec = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "key": key,
        "stream": bool(stream),
        "max_new": max_new,
        "prompt_bytes": len(prompt.encode("utf-8")),
        "n_msgs": len(msgs),
        "tools": [((t.get("function") or {}).get("name") if isinstance(t, dict) else None)
                  for t in (tools or [])],
        "messages": msgs,
    }
    try:
        with open(DUMP_REQ_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                           # noqa: BLE001
        pass


def _drop_unit_bounds(msgs, start):
    """返回从 start 开始、必须**整段一起丢**的非 system 单元 [start, end)。

    tool_use / tool_result 是一对：assistant（带 tool_calls）及其后紧随的所有 tool 结果
    必须同进同出。只丢一半，送到模型的对话就是残缺的（有 result 没 call，或有 call 没
    result），模型会答非所问、甚至寒暄一句就停。孤立的 tool（其 assistant 已被丢）也整段连丢。
    """
    end = start + 1
    role = msgs[start].get("role") or "user"
    if (role == "assistant" and msgs[start].get("tool_calls")) or role == "tool":
        while end < len(msgs) and (msgs[end].get("role") or "") == "tool":
            end += 1
    return end


def fit_window(backend, messages, budget, enable_thinking, tools, min_keep):
    """改动 A：把 messages 裁到 token 预算内（保 system + 最近若干轮）。

    返回 `(裁剪后的 messages, 丢弃条数)`。无需裁剪时返回 `(原 messages, 0)`；
    **预算未知 / 数不出来 / 后端不可用时返回 `(None, 0)`**——调用方（`do_POST`）见到
    None 就按原始 messages 走。这里坚持"宁可不裁，也不要拿一个错的预算去裁对话"：
    不裁最多退回后端原来的 REJECT（客户端收到 400，语义明确），裁错了则是静默给出
    残缺上下文。

    判据用后端 COUNT 帧的精确 token 数（网关没有 tokenizer，字符估算法在中英混排下
    误差很大）。裁剪策略：保留所有 system 消息，从**最老的非 system 单元**开始丢，
    直到装进预算；丢弃以"完整回合"为单位（见 `_drop_unit_bounds`：assistant 的
    tool_calls 与其 tool 结果成对同丢/同留），避免产生残缺对话。丢过东西就插一条占位
    user 消息说明"更早历史已省略"。

    返回的窗口与原 prompt 前缀不同，因此调用方（`do_POST`）会自然地走 RESET 全量
    重发——这正是我们想要的：窗口必须从 KV 空状态重新 prefill 才自洽。
    """
    if backend is None or budget is None or budget <= 0:
        return None, 0
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs:
        return msgs, 0

    def render(seq):
        return render_messages(seq, enable_thinking=enable_thinking, tools=tools)

    total = backend.count(render(msgs))
    if total is None or total <= budget:
        return msgs, 0            # 数不出来或没超预算：按原样

    work = list(msgs)
    dropped_msgs = []
    while True:
        non_sys = [i for i, m in enumerate(work)
                   if (m.get("role") or "user") != "system"]
        if len(non_sys) <= min_keep:
            break
        start = non_sys[0]
        end = _drop_unit_bounds(work, start)
        if len(non_sys) - (end - start) < min_keep:
            break                 # 再丢就跌破 min_keep：整个单元不拆，交给后面的"截断内容"
        dropped_msgs.extend(work[start:end])
        del work[start:end]
        n = backend.count(render(work))
        if n is not None and n <= budget:
            break

    if not dropped_msgs:
        return msgs, 0

    def build(summary):
        content = HISTORY_OMIT_MARKER % len(dropped_msgs)
        if summary:
            content += " 摘要：" + summary
        marker = {"role": "user", "content": content}
        out = []
        inserted = False
        for m in work:
            if not inserted and (m.get("role") or "user") != "system":
                out.append(marker)
                inserted = True
            out.append(m)
        if not inserted:
            out.append(marker)
        return out

    # 摘要本身也占 token：先按初始摘要组装，超预算就把摘要减半；再超就继续丢最老
    # （始终保 min_keep）。这样"裁完一定装得下"不再依赖"摘要恰好很短"这个巧合。
    summary = summarize_dropped(dropped_msgs)
    out = build(summary)
    n = backend.count(render(out))
    guard = 0
    while n is not None and n > budget and summary and guard < 8:
        summary = summary[:len(summary) // 2]
        out = build(summary)
        n = backend.count(render(out))
        guard += 1
    while n is not None and n > budget and guard < 72:
        non_sys = [i for i, m in enumerate(work)
                   if (m.get("role") or "user") != "system"]
        if len(non_sys) <= min_keep:
            break
        start = non_sys[0]
        end = _drop_unit_bounds(work, start)
        if len(non_sys) - (end - start) < min_keep:
            break
        dropped_msgs.extend(work[start:end])
        del work[start:end]
        summary = summarize_dropped(dropped_msgs)
        out = build(summary)
        n = backend.count(render(out))
        guard += 1
    return out, len(dropped_msgs)


def summarize_dropped(msgs, max_chars=600, per_msg=120):
    """改动 C 的"历史摘要"：对窗口丢掉的旧消息做**抽取式**摘要（不用模型）。

    为什么不用模型摘要：NSESSION=2 时跑一次摘要推理要占一个会话、和主对话抢资源，还可能
    搅动正在跑的会话 KV。抽取式零成本、**确定性**（同一窗口每轮渲出同样字节，不破坏 KV
    前缀匹配），足够把"之前聊过什么"这个线索留给后面。
    """
    parts = []
    for m in msgs:
        text = _content_text(m.get("content")).strip().replace("\n", " ")
        if not text:
            continue
        parts.append("%s: %s" % (m.get("role") or "user", text[:per_msg]))
    s = " | ".join(parts)
    if len(s) > max_chars:
        s = s[:max_chars] + "…"
    return s


def _largest_non_system_index(msgs):
    best, best_len = None, -1
    for i, m in enumerate(msgs):
        if (m.get("role") or "user") == "system":
            continue
        n = len(_content_text(m.get("content")))
        if n > best_len:
            best, best_len = i, n
    return best


def _halve_message(msgs, idx, marker="\n...[truncated to fit the context]...\n"):
    """把第 idx 条消息的正文按 head70%+tail30% 砍掉一半（就地替换，保持消息结构）。"""
    m = msgs[idx]
    text = _content_text(m.get("content"))
    if len(text) < 64:
        return False
    keep = len(text) // 2
    head = int(keep * 0.7)
    tail = keep - head
    m2 = dict(m)
    m2["content"] = text[:head] + marker + (text[-tail:] if tail > 0 else "")
    msgs[idx] = m2
    return True


def _slim_schema(schema, level=1):
    """递归精简 JSON Schema：去掉 title/$schema/default/examples，截断 description。

    保留 type / properties / items / required / enum 这些"能不能调对"的关键信息。
    """
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in ("$schema", "title", "default", "examples", "$comment",
                 "additionalProperties", "deprecated"):
            continue
        if k == "description" and isinstance(v, str):
            out[k] = v[:(120 if level <= 1 else 24)]
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: _slim_schema(pv, level) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _slim_schema(v, level)
        elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
            out[k] = [_slim_schema(x, level) for x in v]
        else:
            out[k] = v
    return out


def _min_schema(schema):
    """最简 schema：只留 type / properties(名) / required / enum / items。"""
    if not isinstance(schema, dict):
        return schema
    out = {}
    if "type" in schema:
        out["type"] = schema["type"]
    props = schema.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: _min_schema(v) for k, v in props.items()}
    if isinstance(schema.get("required"), list):
        out["required"] = schema["required"]
    if isinstance(schema.get("enum"), list):
        out["enum"] = schema["enum"]
    if isinstance(schema.get("items"), dict):
        out["items"] = _min_schema(schema["items"])
    return out


def compact_tools(tools, level=1):
    """压缩工具定义但**保持可调用性**：保留 name / parameters，砍掉冗长 description。

    level 1：name + parameters（描述截断），去 title/$schema/default/examples；
    level 2：name + 最简 parameters（连描述都去掉）。

    Claude Code 的工具定义里 description 常占大头；压缩后通常能从几十 KB 降到几 KB，
    从而在 32K 上下文里"留住工具"，而不是被 budget_fit 整块丢弃（丢了模型就无法调工具）。
    """
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            out.append(t)
            continue
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        nf = {"name": name}
        if level <= 1:
            desc = fn.get("description")
            if isinstance(desc, str) and desc:
                nf["description"] = desc[:120]
            nf["parameters"] = _slim_schema(params, level)
        else:
            nf["parameters"] = _min_schema(params)
        out.append({"type": "function", "function": nf})
    return out


def budget_fit(backend, messages, tools, enable_thinking, budget, min_keep):
    """把一条请求**强制**压进 token 预算，同时尽量**保住工具可调用性**。

    返回 `(messages, tools, dropped_msgs, tool_mode, tokens)`；tool_mode ∈
    {"full","compact1","compact2","none"}。后端不可用/无预算时返回 messages=None。

    顺序（先动最不值钱的）：
      1. 丢最老历史（保 system + 近期）；
      2. 仍超 → **压缩工具定义**（level1 砍描述，level2 只留 name+parameters）——
         工具能被调用的关键是 name/parameters，description 只是辅助；**不再一上来就整块丢**；
      3. 仍超 → 逐条截断最大的非 system 消息；
      4. 仍超 → 截断 system；
      5. 万不得已才 `tool_mode="none"` 丢掉全部工具（此时工具已压到最小仍装不下，
         通常只剩"单轮 system 本身就爆"这种极端情况）。

    这样"system+15 个工具 ≈32K"时工具仍会以压缩形态保留，模型照样能发 tool_call；
    而不是像以前那样丢工具 → 模型只能"假装"调用、文件落不了盘。
    """
    if backend is None or budget is None or budget <= 0:
        return None, tools, 0, "full", None
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs:
        return None, tools, 0, "full", None

    use_tools = tools
    tool_mode = "full"
    # 裁剪以"渲染字节数"为主要度尺（不依赖 COUNT，避免把超大文本反复送进 tokenizer），
    # 目标字节按保守的 2.5 字节/token 估，且不超过能被精确计数的大小。
    target_bytes = min(int(budget * 2.5), MAX_COUNT_BYTES)

    def rb(m, t):
        return len(render_messages(m, enable_thinking=enable_thinking, tools=t).encode("utf-8"))

    if rb(msgs, use_tools) <= target_bytes:
        return msgs, use_tools, 0, tool_mode, None

    # 1) 丢最老历史（fit_window 内部会 COUNT；超大时靠 MAX_COUNT_BYTES 走估算）
    w, dropped = fit_window(backend, msgs, budget, enable_thinking, use_tools, min_keep)
    if w is not None:
        msgs = w

    # 2) 压缩工具（保 name/parameters，砍 description）——**不整块丢**
    if use_tools and rb(msgs, use_tools) > target_bytes:
        use_tools = compact_tools(use_tools, level=1)
        tool_mode = "compact1"
    if use_tools and rb(msgs, use_tools) > target_bytes:
        use_tools = compact_tools(use_tools, level=2)
        tool_mode = "compact2"

    # 3) 截断最大的非 system 消息
    guard = 0
    while rb(msgs, use_tools) > target_bytes and guard < 32:
        idx = _largest_non_system_index(msgs)
        if idx is None or not _halve_message(msgs, idx):
            break
        guard += 1

    # 4) 截断 system
    guard = 0
    while rb(msgs, use_tools) > target_bytes and guard < 16:
        si = next((i for i, m in enumerate(msgs)
                   if (m.get("role") or "") == "system"), None)
        if si is None or not _halve_message(msgs, si):
            break
        guard += 1

    # 5) 最后手段才丢工具
    if use_tools and rb(msgs, use_tools) > target_bytes:
        use_tools = []
        tool_mode = "none"

    # 最后用真实 COUNT 确认（此时字节 ≤ MAX_COUNT_BYTES，计数是精确的）；仍超就再截。
    n = backend.count(render_messages(msgs, enable_thinking=enable_thinking, tools=use_tools))
    guard = 0
    while n is not None and n > budget and guard < 32:
        idx = _largest_non_system_index(msgs)
        si = next((i for i, m in enumerate(msgs)
                   if (m.get("role") or "") == "system"), None)
        target = idx if idx is not None else si
        if target is None or not _halve_message(msgs, target):
            break
        n = backend.count(render_messages(msgs, enable_thinking=enable_thinking,
                                          tools=use_tools))
        guard += 1
    return msgs, use_tools, dropped, tool_mode, n


# ===========================================================================
# 4. HTTP / OpenAI
# ===========================================================================

# 演示页（多路对话框那个，窗口数由页面自己问 /health 决定）与网关同目录，用 GET /demo 直接打开。
# 为什么由网关自己发而不是另起一个静态服务器：**同源**，浏览器就不会有 CORS 的坑，
# 演示时也只有一个进程要管。
VIEW_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_PAGE = "demo_4chat.html"

# 跨源放行。**默认一个 CORS 头都不发**（2026-09-20 改）。
#
# 以前这里是写死的 `Access-Control-Allow-Origin: *`。那个值对这套服务是**纯风险**：
# 这个 API **没有任何鉴权**，而 ACAO 是浏览器唯一的门禁 —— `*` 等于对外宣布"任何网页
# 都可以读我的响应"，于是局域网里**任何人打开的任意网页**都能背地里驱动这块板卡
# （读 `/v1/system`、发起生成、拿 `/v1/pool` 给的 key 把别人的对话 close 掉）。
# 而演示页是**同源**的（同一个 host:port 发出来的），一个 CORS 头都用不上：
# 同源请求本来就不看 ACAO，`X-KV-Reuse` 这种自定义响应头同源下也是默认可读的。
# 真要让别的域名（比如另挂一个前端）来用，就显式 `--cors-origin https://那个域名`，
# 而且是**精确匹配**、只回给匹配上的那一个来源，不做子串/通配。
def cors_headers(allowed, origin):
    """按"配置里允许的来源"和"这次请求带的 Origin"算出该发的 CORS 头。

    默认参数（`allowed=None`）**永远返回空**——这是这条防线唯一重要的性质，
    自检里专门钉了它（改回 `*` 必须让自检变红）。
    """
    if not allowed or not origin:
        return {}
    if allowed == "*" or origin == allowed:
        return {
            # 允许别人用就回它的来源；`*` 是运维自己显式写的，照它说的回。
            "Access-Control-Allow-Origin": "*" if allowed == "*" else origin,
            # 浏览器默认**不让 JS 读自定义响应头**，这两个必须显式 expose 出来，否则
            # 页面上"复用到底有没有生效"这一栏、以及「暂停」要用的 request id 都读不到。
            "Access-Control-Expose-Headers": "X-KV-Reuse, X-Request-Id",
        }
    return {}


def _perf_summary(vals):
    """一组数的分位数摘要（最近秩法）。空集合 => 全 None，不编 0。"""
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "mean": None, "max": None}
    s = sorted(vals)

    def pick(p):
        idx = int(round((p / 100.0) * (len(s) - 1)))
        return round(s[max(0, min(len(s) - 1, idx))], 3)

    return {"n": len(s), "p50": pick(50), "p90": pick(90), "p99": pick(99),
            "mean": round(sum(s) / len(s), 3), "max": round(s[-1], 3)}


class PerfCollector(object):
    """性能记录与聚合（2026-09-29 加）。

    为什么放在网关进程里：一次请求最关键的两个量——prefill / decode 各自的 token 数
    与耗时——只有帧协议的 DONE 帧上有；HTTP 客户端能测 TTFT 与 E2E，但拿不到这个拆分。
    网关把它们连同排队、KV 复用一起记下来，`/v1/perf` 给出窗口内的分位数与总吞吐。
    调度算法 A/B（FCFS vs MLFQ）读同一个接口、同一个口径，不依赖日志解析。

    口径（写进文档、与算法侧一致，避免各自解读）：
      · ttft_ms      = REQ 写出 → 第一段非空文本（服务器侧，**不含排队**）
      · e2e_ms       = REQ 写出 → DONE（服务器侧，**不含排队**）
      · queue_wait_s = 会话池排队秒数（客户端算不出来；对应 X-KV-Reuse 的 wait）
      · prefill/decode_tok_s = DONE 帧的真实 token 数 ÷ 该阶段耗时
      · bubble_ratio = 1 − Σ会话忙时 ÷ (N × 窗口跨度)，窗口 = [min(t_submit), max(t_done)]
        会话忙时用 e2e_ms 近似（REQ 发出即占着会话，直到 DONE）——与"槽位空转率"同义。
    """

    MAX_RECORDS = 10000

    def __init__(self):
        self._lock = threading.Lock()
        self._records = collections.deque(maxlen=self.MAX_RECORDS)

    def add(self, rec):
        with self._lock:
            self._records.append(rec)

    def reset(self):
        with self._lock:
            n = len(self._records)
            self._records.clear()
            return n

    def snapshot(self, n_sessions=None, full=False):
        with self._lock:
            recs = list(self._records)
        ok = [r for r in recs if r.get("ok")]
        pf_tok = sum(r.get("prefill_tokens", 0) for r in ok)
        pf_ms = sum(r.get("prefill_ms", 0.0) for r in ok)
        dc_tok = sum(r.get("decode_tokens", 0) for r in ok)
        dc_ms = sum(r.get("decode_ms", 0.0) for r in ok)
        cached = sum(r.get("cached_tokens", 0) for r in ok)
        prompt = sum(r.get("prompt_tokens", 0) for r in ok)
        subs = [r.get("t_submit") for r in recs if r.get("t_submit") is not None]
        dones = [r.get("t_done") for r in recs if r.get("t_done") is not None]
        span = (max(dones) - min(subs)) if subs and dones else None
        busy = sum(r["e2e_ms"] for r in ok if r.get("e2e_ms") is not None) / 1000.0
        busy_ratio = None
        if span and span > 0 and n_sessions:
            busy_ratio = min(1.0, busy / (span * n_sessions))
        finish = {}
        for r in recs:
            key = r.get("finish") or ("rejected" if r.get("rejected")
                                      else ("error" if not r.get("ok") else "?"))
            finish[key] = finish.get(key, 0) + 1
        doc = {
            "sessions": n_sessions,
            "window": {
                "n": len(recs), "ok": len(ok),
                "aborted": sum(1 for r in ok if r.get("finish") == "abort"),
                "rejected": sum(1 for r in recs if r.get("rejected")),
                "errors": sum(1 for r in recs if not r.get("ok") and not r.get("rejected")),
                "span_s": round(span, 3) if span else None,
                "finish": finish,
            },
            "ttft_ms": _perf_summary([r.get("ttft_ms") for r in ok]),
            "e2e_ms": _perf_summary([r.get("e2e_ms") for r in ok]),
            "queue_wait_s": _perf_summary([r.get("queue_wait_s") for r in recs]),
            "prefill": {
                "tokens": pf_tok, "ms": round(pf_ms, 1),
                "tok_s": round(pf_tok / (pf_ms / 1000.0), 1) if pf_ms > 0 else None,
                "rate_p50": _perf_summary([r.get("prefill_tok_s") for r in ok])["p50"],
            },
            "decode": {
                "tokens": dc_tok, "ms": round(dc_ms, 1),
                "tok_s": round(dc_tok / (dc_ms / 1000.0), 2) if dc_ms > 0 else None,
                "rate_p50": _perf_summary([r.get("decode_tok_s") for r in ok])["p50"],
                "tpot_ms_p50": _perf_summary([r.get("tpot_ms") for r in ok])["p50"],
            },
            "kv": {
                "prompt_tokens": prompt, "cached_tokens": cached,
                "cache_hit_ratio": round(cached / prompt, 4) if prompt else None,
            },
            "slots": {
                "busy_ratio": round(busy_ratio, 4) if busy_ratio is not None else None,
                "bubble_ratio": (round(max(0.0, 1.0 - busy_ratio), 4)
                                 if busy_ratio is not None else None),
            },
        }
        if full:
            doc["requests"] = recs
        return doc


class Gateway(object):
    def __init__(self, backend, pool, verbose=False, cors_origin=None,
                 temp_file=_CARD_TEMP_FILE, window_enable=True,
                 ctx_reserve=DEFAULT_CTX_RESERVE,
                 window_min_messages=DEFAULT_WINDOW_MIN_MESSAGES,
                 window_max_output=DEFAULT_WINDOW_MAX_OUTPUT):
        self.backend = backend
        self.pool = pool
        self.verbose = verbose
        # 允许哪个来源跨源调用（None = 一个都不允许）。见 cors_headers()。
        self.cors_origin = cors_origin
        # RK1828 结温的来源（另一个 root 进程写的文件，见 _read_card_temps）。
        self.temp_file = temp_file
        # 改动 A：窗口裁剪的开关与参数（见 fit_window）。
        self.window_enable = window_enable
        self.ctx_reserve = ctx_reserve
        self.window_min_messages = window_min_messages
        self.window_max_output = window_max_output
        self.started_at = time.time()
        self.host = HostSampler()
        self.cards = CardStats()
        # 性能统计（2026-09-29）：每条完成/失败的请求一条记录，`/v1/perf` 读它。
        # 是**进程内**的滚动窗口（默认最近 10000 条），不落盘、重启即清。
        self.perf = PerfCollector()
        # /v1/system 的快照缓存（见 SYS_CACHE_S）。锁的作用不只是保护这两个字段：
        # **同一时刻只有一个人去采**，后来的那个等在外面拿现成的——采一次要读十几个
        # 文件、还要向后端要一帧，放进来两个人一起采等于把省下来的又花回去。
        self._sys_lock = threading.Lock()
        self._sys_doc = None
        self._sys_at = 0.0

    def log(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("[gateway] " + (fmt % args) + "\n")
            sys.stderr.flush()

    def system_snapshot(self, max_age=SYS_CACHE_S):
        """`/v1/system` 的内容（带 `max_age` 秒的快照缓存，见 SYS_CACHE_S）。

        `max_age=0` = 不要缓存、现在就采一份（自检与排障用）。
        """
        with self._sys_lock:
            now = time.monotonic()
            if self._sys_doc is not None and (now - self._sys_at) < max_age:
                return self._sys_doc
            doc = self._system_snapshot_now()
            # 时间戳取**采完**的时刻，不是开始采的时刻：采这一份本身可能要等 STAT 一秒钟，
            # 按开始时刻算的话它一出锁就是过期的，缓存等于不存在。
            self._sys_doc = doc
            self._sys_at = time.monotonic()
            return doc

    def _system_snapshot_now(self):
        """RK3588（本机读）+ RK1828（后端 STAT 帧）+ 网关自己。

        **两半的可信度不一样，所以不用同一个形状糊在一起**：`host` 是这一秒读到的，
        `cards` 是从后端来的、带着它的采样时刻；`backend_stats_age_s` 说后者有多旧。
        """
        doc = self.backend.request_stats()
        temps, temps_at = _read_card_temps(self.temp_file)
        cards = self.cards.sample(doc, temps, temps_at)
        stats_age = (round(time.monotonic() - self.backend.stats_at, 1)
                     if self.backend.stats_at else None)
        pool = self.pool.snapshot()
        return {
            "ts": round(time.time(), 3),
            "host": self.host.sample(),
            "npu": cards,
            "backend": {
                "alive": self.backend.alive(),
                "sessions": self.backend.sessions,
                "ctx_size": self.backend.ctx_size,
                "stats_age_s": stats_age,
            },
            "gateway": {
                "busy": sum(1 for s in pool["slots"] if s["state"] == "busy"),
                "bound": sum(1 for s in pool["slots"] if s["state"] in ("busy", "idle")),
                "dead": sum(1 for s in pool["slots"] if s["state"] == "dead"),
                "waiting": len(pool["waiting"]),
                "uptime_s": round(time.time() - self.started_at, 1),
                # KV 换页观测（改动 C / E1a）：冷会话数、换页存储字节、累计与最近一次耗时/带宽。
                "swapped": pool.get("swapped"),
                "swapped_keys": pool.get("swapped_keys"),
                "swap_bytes": pool.get("swap_bytes"),
                # 活跃会话的身份标签（来自 pool.slots[].key）；与 swapped_keys 一起标注历史条目状态。
                "active_keys": [s.get("key") for s in pool.get("slots", []) if s.get("key")],
                "swap_out_total": pool.get("swap_out_total"),
                "swap_in_total": pool.get("swap_in_total"),
                "last_swap_out_ms": pool.get("last_swap_out_ms"),
                "last_swap_out_mb_s": pool.get("last_swap_out_mb_s"),
                "last_swap_in_ms": pool.get("last_swap_in_ms"),
                "last_swap_in_mb_s": pool.get("last_swap_in_mb_s"),
            },
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rkllm-openai-gateway/1.0"
    # 读请求行的超时。默认（None = 不超时）+ ThreadingHTTPServer 是一个没有上限的组合：
    # 只连上、一个字节都不发的客户端会把那一个线程**永久**占住（每个连接一个线程，没有
    # 上限），几百个空连接就能把内存吃干。超时后按默认行为关连接、放掉线程。
    # 注意它只作用于"读下一次请求"，不作用于我们正在生成的那一轮——生成期间我们在写、
    # 不在读，长上下文一轮跑几百秒也不会被它打断。
    timeout = 60.0

    gateway = None          # 由 main() 注入

    def log_message(self, fmt, *args):
        gw = self.gateway
        if gw is not None and gw.verbose:
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具 ----

    def _cors(self):
        """这一次请求该发的 CORS 头。默认是**空字典**（不配置 `--cors-origin` 时）。

        判定放在这里（而不是每个响应处各判一次），是为了让"默认不放行"这件事只有一处
        可改——想改回 `*` 就得动 cors_headers()，而那里有自检钉着。
        """
        gw = self.gateway
        if gw is None:
            return {}
        return cors_headers(gw.cors_origin, self.headers.get("Origin"))

    def _json(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for k, v in self._cors().items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._responded = True

    def _error(self, code, message, err_type="invalid_request_error"):
        self._json(code, {"error": {"message": message, "type": err_type, "code": code}})

    def _fail(self, code, message, err_type):
        """出错收尾。响应已经开始写（比如 SSE 头已发出）时**不能**再写一个 HTTP 响应，
        那只会往流里塞垃圾让客户端解析崩掉；直接断开连接即可。"""
        if self._responded:
            if self.gateway is not None:
                self.gateway.log("error after response started: %s", message)
            self.close_connection = True
            return
        self._error(code, message, err_type)

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # ---- 路由 ----

    def do_OPTIONS(self):
        """CORS 预检。

        带 `Content-Type: application/json` 的 POST **不是**简单请求，浏览器会先发一个
        OPTIONS 问一句"能不能发"。不处理这一条，页面上每个请求都会在控制台里失败，
        而在 curl/python 那一侧完全看不出来——因为它们压根不发预检。

        **没配置 `--cors-origin` 时就老老实实回一个空的 204**：浏览器拿不到
        `Access-Control-Allow-Origin`，预检就是不通过，这才是"不允许跨源"该有的样子。
        演示页是同源的，同源请求**不发预检**，所以这条路径现在只服务想跨源来用的前端。
        """
        self._responded = False
        self.send_response(204)
        cors = self._cors()
        for k, v in cors.items():
            self.send_header(k, v)
        if cors:            # 不放行时这三个头一个都不用发（发了也没人认，还多余）
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            # X-Conversation-Id 必须列进来，否则浏览器里发这个头会被预检拦掉（curl/python
            # 不发预检，所以这个问题只在网页端现形，很容易查错方向）。
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-Conversation-Id")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self._responded = True

    def _static_demo(self):
        """把演示页发出去。

        页面从这里取就是同源的（地址栏和接口都是同一个 host:port），CORS 这一关天然过。
        页面被另存到本地用 `file://` 打开就跨源了——那种用法要显式给 `--cors-origin`，
        默认不再顺手放行（见 cors_headers 上面那段）。
        """
        path = os.path.join(VIEW_DIR, DEMO_PAGE)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except (IOError, OSError):
            return self._error(404, "demo page missing: %s (应与网关放在同一目录)" % path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._responded = True

    def _stop_generation(self):
        """`POST /v1/stop`：停掉在跑的那一轮。

        body 给 `{"request_id": N}`，N 就是 `_stream_response` 回在 `X-Request-Id`
        头里的那个数（后端的 rid，网关进程内单调、不复用）。

        这一条**不校验 rid 是否真的在跑**：后端对「不认识的 rid」是静默的，这里再去
        查一轮、回一个「没停到」只会多一种网页要处理的状态；而有没有停到，网页本来
        就会从那一轮的 DONE（finish_reason=abort）看到。所以这里只回「STOP 已发出」。
        """
        req = self._read_json()
        if req is None:
            return self._error(400, "body is not valid JSON")
        try:
            rid = int(req.get("request_id"))
        except (TypeError, ValueError):
            return self._error(400, "request_id (integer) is required")
        self.gateway.backend.stop_request(rid)
        self.gateway.log("stop: rid=%d", rid)
        return self._json(200, {"request_id": rid, "sent": True})

    def _continue_response(self, req):
        """`POST /v1/chat/completions` 带 `{"continue": true}`：继续上一轮被打断的回答。

        body 里除 continue 外带 conversation_id（或 user / X-Conversation-Id）与
        max_tokens。messages 不需要（RESUME 不 prefill），但身份**必须**和被打断的那
        一轮一致，否则 acquire_continue 找不到会话。响应始终流式——「继续生成」天然
        是流式的，且网页端就是这么读的。
        """
        key = conversation_key([], explicit=req.get("conversation_id")
                               or self.headers.get("X-Conversation-Id")
                               or req.get("user"))
        max_new = _pick_max_tokens(req)
        ctk = req.get("chat_template_kw") or {}
        enable_thinking = ctk.get("enable_thinking") if isinstance(ctk, dict) else None

        lease = None
        q = None
        try:
            lease = self.gateway.pool.acquire_continue(key)
            q = self.gateway.backend.resume(lease.session, max_new)
            base_len = len((lease.base or "").encode("utf-8"))
            q.kv = {"session": lease.session, "reset": 0, "sent": 0,
                    "base": base_len, "full": base_len, "wait": lease.waited}
            self.gateway.log("continue: session=%d max_new=%d%s", lease.session,
                             max_new, (" 排队 %.1fs" % lease.waited)
                             if lease.waited >= 0.05 else "")
            self._stream_response(req, q, lease, enable_thinking)
        except BackendError as exc:
            if exc.soft:
                self._fail(400, str(exc), "invalid_request_error")
            else:
                self._fail(503, str(exc), "server_error")
        except Exception as exc:                        # noqa: BLE001
            self._fail(500, "%r" % (exc,), "server_error")
        finally:
            if lease is not None:
                failed = q is None or q.error or q.dead
                if failed:
                    self.gateway.pool.mark_dead(lease.session)
                    self.gateway.pool.release(lease, "", True)
                else:
                    self.gateway.pool.release(
                        lease, assistant_continuation(q, enable_thinking),
                        q.was_cleared)
            if q is not None:
                self.gateway.backend.release_request(q)

    def do_GET(self):
        self._responded = False
        path = self.path.split("?")[0]
        if path in ("/", "/demo", "/demo_4chat.html"):
            return self._static_demo()
        if path in ("/health", "/healthz"):
            pool = self.gateway.pool.snapshot()
            dead = sum(1 for s in pool["slots"] if s["state"] == "dead")
            backend_alive = self.gateway.backend.alive()
            return self._json(200, {"status": ("ok" if backend_alive and dead == 0 else
                                               "backend_exited" if not backend_alive else
                                               "degraded"),
                                    "model": MODEL_ID,
                                    "sessions": self.gateway.backend.sessions,
                                    # 后端真正拿到的 --ctx-size。页面要用它把"窗口数
                                    # 上限由上下文长度决定"这条关系讲给用户听
                                    # （KV 每路按满上下文预分配：32K 每路 406.7 MB/卡，
                                    # 每卡只够 2 路；8K 那份导出便宜得多）。取不到就是 null，
                                    # 页面会自己退化成不显示这一格。
                                    "ctx_size": self.gateway.backend.ctx_size,
                                    # 「还能不能干活」必须能从这一行看出来。现场最坑的一种
                                    # 状态是 status:"ok" + sessions:4 + 四个 slot 全 dead：
                                    # 后端子进程已经退出、每个请求都 503，而 /health 报的是
                                    # 满员。看护脚本本来就在轮询这里，别让它去学 /v1/pool。
                                    "backend_alive": backend_alive,
                                    "sessions_dead": dead,
                                    # 会话全被占 + 有人在等 = 到上限了。放进 /health 是因为
                                    # 监控/看护脚本本来就在轮询这个端点，不必再教它一个新路径。
                                    "sessions_busy": sum(1 for s in pool["slots"]
                                                         if s["state"] == "busy"),
                                    # busy + idle = 有主的（free 才是没人占；dead 的会话
                                    # 在 mark_dead 里已经被解绑了，所以不会是 dead+有主）
                                    "sessions_bound": sum(1 for s in pool["slots"]
                                                          if s["state"] in ("busy", "idle")),
                                    "waiting": len(pool["waiting"]),
                                    "uptime_s": round(time.time() - self.gateway.started_at, 1)})
        if path == "/v1/system":
            # 非 OpenAI 标准端点，纯观测：板卡此刻的 CPU / 内存 / 温度 / 每卡 NPU。
            # 页面每 2s（待机 6s）轮一次，网关侧还有 SYS_CACHE_S 秒的快照缓存（几个人同时
            # 开着页面也只采一次）；**这个端点永远不会因为后端卡住而报错**——拿不到新数就
            # 返回上一份并带上年龄，因为"看板卡现在怎么样"正是它卡住时最需要的。
            try:
                return self._json(200, self.gateway.system_snapshot())
            except Exception as exc:                        # noqa: BLE001
                return self._error(500, "system snapshot failed: %r" % (exc,),
                                   err_type="server_error")
        if path == "/v1/pool":
            # 非 OpenAI 标准端点，纯观测：谁占着哪个会话、闲了多久、谁在排队。
            return self._json(200, self.gateway.pool.snapshot())
        if path == "/v1/perf":
            # 非 OpenAI 标准端点，纯观测：性能统计窗口（调度算法 A/B 的统一尺子）。
            # 默认只回聚合；`?full=1` 连每条明细一起回（离线画图/回归用）。
            full = ("full=1" in self.path) or ("full=true" in self.path)
            return self._json(200, self.gateway.perf.snapshot(
                self.gateway.backend.sessions, full=full))
        if path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{
                "id": MODEL_ID, "object": "model",
                "created": int(self.gateway.started_at), "owned_by": "local"}]})
        return self._error(404, "unknown path: %s" % path)

    def _close_conversation(self):
        """`POST /v1/conversations/close`：把这段对话占的会话立刻交还（不等 `IDLE_TTL`）。

        body 给 `conversation_id`（或 `user`），或只给 `X-Conversation-Id` 头——与
        `/v1/chat/completions` 同一套身份优先级。

        **匿名对话要用 `key` 关**（`{"key": "h:faa51b39..."}`，键从 `GET /v1/pool` 的
        `slots[].key` 读）：匿名请求的身份是网关按 system+首问算的内容摘要，客户端自己
        算不出来，不给它一条路的话那段对话会一直占到 `IDLE_TTL` 超时。**只给了身份没法
        关的那种情况**（既没 id 也没 key）回 400，把原因写清楚，别让人以为是网关坏了。
        """
        req = self._read_json()
        if req is None:
            req = {}
        raw_key = req.get("key")
        explicit = (req.get("conversation_id")
                    or self.headers.get("X-Conversation-Id")
                    or req.get("user"))
        if raw_key:
            key = str(raw_key)
        elif explicit:
            key = conversation_key([], explicit=explicit)
        else:
            return self._error(400, "conversation_id (or X-Conversation-Id / user) is "
                                    "required; anonymous conversations must be closed by "
                                    "their raw `key` from GET /v1/pool, because their id "
                                    "is derived from message content")
        try:
            session = self.gateway.pool.close(key)
        except BackendError as exc:
            return self._error(409, str(exc), "server_error")
        self.gateway.log("close: %s -> %s", explicit or key, session)
        return self._json(200, {"conversation_id": str(explicit) if explicit else None,
                                "key": key,
                                "closed": session is not None,
                                "session": session})

    def _prompt_budget(self, max_new):
        """改动 A 的 prompt token 预算 = 模型上下文上限 − 余量 − 本轮输出预留。

        ctx_size 取模型内建上限（后端 `/health` 报的那个数，与 backend.ctx_size 同源）；
        拿不到就返回 None（不裁剪，退回旧行为）。输出预留按请求的 max_tokens 估，但对它
        封顶：客户端常用 `"max_tokens": 100000` 表示"能吐多少吐多少"，真按它扣预算会把
        prompt 挤到几乎没空间——那正是要避免的。封顶到 min(请求值, 固定上限, 上下文/4)。
        """
        backend = self.gateway.backend
        # 旧后端（READY 只有两个字段）不认识 COUNT 帧：一旦发过去会等满超时。直接返回
        # None = 不做窗口裁剪，退回"超预算就 400"的旧行为（至少不会把服务拖死）。
        if backend is None or not getattr(backend, "protocol_v2", False):
            return None
        limit = (getattr(backend, "model_ctx_size", None)
                 or getattr(backend, "ctx_size", None))
        if not limit or limit <= 0:
            return None
        out = max_new if max_new and max_new > 0 else (backend.default_max_new_tokens or 0)
        out = min(out, self.gateway.window_max_output, max(1, limit // 4))
        budget = limit - self.gateway.ctx_reserve - out
        return budget if budget > 0 else None

    def do_POST(self):
        self._responded = False
        path = self.path.split("?")[0]
        if path == "/v1/conversations/close":
            return self._close_conversation()
        if path == "/v1/stop":
            return self._stop_generation()
        if path == "/v1/perf/reset":
            # A/B 测量的起跑线：先 reset，再打负载，跑完读一次 /v1/perf。
            return self._json(200, {"reset": self.gateway.perf.reset()})
        if path != "/v1/chat/completions":
            return self._error(404, "unknown path: %s" % path)
        req = self._read_json()
        if req is None:
            return self._error(400, "body is not valid JSON")
        if req.get("continue"):
            # 「继续回答」：不 render 任何 prompt，直接让后端接着上一轮 decode。
            return self._continue_response(req)
        messages = req.get("messages")
        if not messages:
            return self._error(400, "messages is required")

        stream = bool(req.get("stream"))
        max_new = _pick_max_tokens(req)
        ctk = req.get("chat_template_kw") or {}
        enable_thinking = ctk.get("enable_thinking") if isinstance(ctk, dict) else None
        tools = _pick_tools(req)
        if _TOOL_STABLE:
            tools = normalize_tools(tools)
        # 改动 A：超预算就先裁剪（保 system + 近期），否则原样渲染。key 仍按**原始**
        # messages 计算（用 system + 首条 user），窗口丢掉首条 user 也不会改身份。
        prompt = render_messages(messages, enable_thinking=enable_thinking, tools=tools)
        dropped = 0
        tool_mode = "full"
        if self.gateway.window_enable:
            budget = self._prompt_budget(max_new)
            # bytes>budget 只是"保守触发"：token 数 ≤ 字节数，字节超了不代表 token 也超。
            # 中英混排下 bytes/token≈3–4，直接裁会大量误判——前缀一变，下一轮就无法复用
            # KV，整段重算 prefill，NPU 被反复 prefill 打满。这里先用后端 COUNT 精算确认：
            # 未超预算就原样发（保住粘性复用），确超了才走降级裁剪。
            if budget is not None and len(prompt.encode("utf-8")) > budget:
                n_now = self.gateway.backend.count(prompt)
                if not (n_now is not None and n_now <= budget):
                    fmsgs, ftools, dropped, tool_mode, _ntok = budget_fit(
                        self.gateway.backend, messages, tools, enable_thinking, budget,
                        self.gateway.window_min_messages)
                    if fmsgs is not None:
                        prompt = render_messages(fmsgs, enable_thinking=enable_thinking,
                                                 tools=ftools)
                        tools = ftools
        # 会话身份，优先级从高到低：
        #   1. 请求体的 conversation_id —— 本项目自己的扩展，语义最明确
        #   2. X-Conversation-Id 请求头 —— 同上，给不方便改 body 的客户端
        #   3. OpenAI 标准的 user 字段 —— 多用户场景直接写用户名就行，不用教调用方新东西
        #   4. 都没有 => 退回"按 system + 首条提问做摘要"
        # 第 4 条在多人场景下**是会撞的**：两个人问同一句话就是同一段对话，会被钉到同一个
        # 会话上串行（答案照对，吞吐掉一半）。所以多人接入时务必显式给前三者之一。
        key = conversation_key(messages, explicit=req.get("conversation_id")
                               or self.headers.get("X-Conversation-Id")
                               or req.get("user"))
        self.gateway.log("chat: %d messages, %d prompt bytes, stream=%s, max_new=%d, tools=%d%s%s",
                         len(messages), len(prompt.encode("utf-8")), stream, max_new,
                         len(tools),
                         ("; window dropped %d msgs" % dropped) if dropped else "",
                         ("; tools %s" % tool_mode) if tool_mode != "full" else "")
        dump_request(req, key, prompt, max_new, tools, stream)

        lease = None
        q = None
        try:
            lease = self.gateway.pool.acquire(
                key, prompt, max_new=max_new,
                cls=_hdr_int(self.headers.get("X-Priority"), 0),
                wq=_hdr_float(self.headers.get("X-Weight"), 1.0),
                deadline=_hdr_deadline(self.headers.get("X-Deadline-Ms")))
            q = self.gateway.backend.submit(prompt=lease.sent_prompt,
                                            max_new_tokens=max_new,
                                            session=lease.session,
                                            reset=lease.reset)
            q.kv = {"session": lease.session, "reset": lease.reset,
                    "sent": len(lease.sent_prompt.encode("utf-8")),
                    "base": len(lease.base.encode("utf-8")),
                    "full": len(prompt.encode("utf-8")),
                    "swapped": 1 if getattr(lease, "swapped", False) else 0,
                    "wait": lease.waited}
            self.gateway.log("lease: session=%d reset=%s sent=%d/%d bytes (base=%d swapped=%d)%s",
                             q.kv["session"], q.kv["reset"], q.kv["sent"], q.kv["full"],
                             q.kv["base"], q.kv["swapped"],
                             # 排过队就一定要打出来：**"慢"和"没轮到"在日志里必须分得开**，
                             # 否则事后只能靠 pool 快照的时间线去猜（见 CHANGELOG 09-18）。
                             (" 排队 %.1fs" % lease.waited) if lease.waited >= 0.05 else "")
            if stream:
                self._stream_response(req, q, lease, enable_thinking)
            else:
                self._blocking_response(req, q, enable_thinking)
        except BackendError as exc:
            if exc.soft:
                # 后端拒了这一轮（上下文装不下）：这是**客户端要改请求**的错误，不是
                # 服务端故障。回 503 的话客户端会原样重试，而原样重试必然再被拒一次。
                self._fail(400, str(exc), "invalid_request_error")
            else:
                self._fail(503, str(exc), "server_error")
        except Exception as exc:                        # noqa: BLE001
            self._fail(500, "%r" % (exc,), "server_error")
        finally:
            # 无论成功、失败还是客户端断开，都要把会话还回去；推理失败还要把会话标死。
            if lease is not None:
                if q is not None and q.rejected:
                    # 后端在 prefill 前拒了这一轮：会话**没死**。标死它就等于每撞一次
                    # 上下文超限就永久少一个会话。而且这一轮一个 token 都没进 KV，
                    # 记账也不能走 release（见 pool.discard）。
                    self.gateway.pool.discard(lease)
                else:
                    failed = q is None or q.error or q.dead
                    if failed:
                        self.gateway.pool.mark_dead(lease.session)
                        self.gateway.pool.release(lease, "", True)
                    else:
                        # 记账用的必须是**客户端会回显的那份文本**：下一轮 prompt 是这个
                        # 版本的拼接，前缀判据才有可能成立。用 q.text()（原始版）记的话，
                        # 关思考时每轮都会判"对不上"，缓存静默全废——而且答案是对的，
                        # 看不出来（nothink_check.py 抓的就是这个）。工具调用把这条规则
                        # 又抬高一档：带着 tool_calls 的助手轮在下一轮 prompt 里是
                        # 「正文 + <tool_call> XML」，所以记账也必须是渲染后的那一份，
                        # 而不是模型原始输出（见 assistant_continuation）。
                        self.gateway.pool.release(
                            lease, assistant_continuation(q, enable_thinking),
                            q.was_cleared)
            if q is not None:
                self.gateway.backend.release_request(q)

    # ---- 两种响应形态 ----

    def _stream_response(self, req, q, lease, enable_thinking=None):
        created = int(time.time())
        cid = "chatcmpl-%d-%d" % (created, q.rid)
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        # 关思考时把开头那个（空的）think 段摘掉，两种响应形态共用同一份状态机
        stripper = ThinkStripper() if enable_thinking is False else None
        extractor = ToolCallExtractor(reasoning=reply_reasoning(enable_thinking))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in self._cors().items():
            self.send_header(k, v)
        for k, v in kv_reuse_headers(q.kv).items():
            self.send_header(k, v)
        # 网页要「暂停回答」得先知道**这一轮是哪个 rid**：把它回在头里，网页在
        # 读 body 之前先读这个头，等会 POST /v1/stop 时原样带回来。用头而不是塞进
        # 第一个 SSE chunk：chunk 得等 body 流起来才有，而头在 fetch 一返回就拿到了。
        self.send_header("X-Request-Id", str(q.rid))

        def sse(obj):
            if q.client_gone:
                raise BackendError("client disconnected")
            try:
                self._chunk(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")
                            .encode("utf-8"))
            except Exception:
                # 写不出去 = 客户端断了：标记 + 立刻叫停后端，避免板卡白跑完这一轮。
                q.client_gone = True
                q.on_delta = None
                try:
                    self.gateway.backend.stop_request(q.rid)
                except Exception:                      # noqa: BLE001
                    pass
                raise

        def delta_obj(content, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": MODEL_ID,
                    "choices": [{"index": 0,
                                 "delta": ({"content": content} if content is not None else {}),
                                 "finish_reason": finish}]}

        self.end_headers()
        self._responded = True          # 头已发出：后面出错只能断连，不能再写响应
        # 首个 chunk 按惯例只带 role
        sse({"id": cid, "object": "chat.completion.chunk", "created": created,
             "model": MODEL_ID,
             "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})

        sent = [0]
        n_calls = [0]

        def tool_delta(call):
            """一条工具调用的 SSE 增量。

            `arguments` 一次性发完，不按 token 切片：客户端是按 `index` 把同一路调用的
            arguments 拼起来的，一次给全和分片给在协议上等价。分片要在网关侧多维护一份
            "这个 JSON 字符串切到哪了"的状态，换来的只是首字节早一点——不值得为它多一份
            会出错的逻辑。
            """
            idx = n_calls[0]
            n_calls[0] += 1
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": MODEL_ID,
                    "choices": [{"index": 0,
                                 "delta": {"tool_calls": [{
                                     "index": idx,
                                     "id": "call_%d" % idx,
                                     "type": "function",
                                     "function": {
                                         "name": call["name"],
                                         "arguments": json.dumps(
                                             call.get("arguments") or {},
                                             ensure_ascii=False)}}]},
                                 "finish_reason": None}]}

        def emit(text):
            """统一出口：过 stripper（没开就是原样）再摘工具调用，各自非空才发。"""
            if stripper is not None:
                text = stripper.feed(text)
            if not text:
                return
            text, calls = extractor.feed(text)
            if text:
                sse(delta_obj(text))
                sent[0] += 1
            for call in calls:
                sse(tool_delta(call))

        def on_delta(text):
            emit(text)

        # 接手：与 reader 线程在 deliver_lock 里交接，保证"等待期间攒下的那一段"
        # 恰好交一次。两点都不能省：
        #   · 只补发 raw 里 delivered 之后的字节，不是整段 raw——整段重发正是
        #     "首段内容重复"的来源（reader 可能正好在 attach 的瞬间投过一段）；
        #   · 补发也走 q.decoder（增量解码器），因为跨帧切开的 UTF-8 字符要靠它拼回来；
        #     整段 decode 会在切缝处吐出替换字符，那个字符就**消失**了。
        with q.deliver_lock:
            q.on_delta = on_delta
            backlog = b""
            if q.delivered < len(q.raw):
                backlog = q.decoder.decode(bytes(q.raw[q.delivered:]))
                q.delivered = len(q.raw)
        if backlog:
            emit(backlog)
        q.done.wait(REQUEST_TIMEOUT)
        if q.client_gone:
            # 客户端已经断开、后端也已叫停：不再尝试写响应（会再次抛异常）。
            self.gateway.log("client gone: rid=%d session=%d (backend stopped)",
                             q.rid, q.session)
            return
        if not q.done.is_set():
            self.gateway.pool.mark_dead(lease.session)
            self._record_perf(q)        # 先记账，再收尾（客户端看到流结束即能查到）
            self._chunk(b"")            # 收尾：别让客户端一直挂着
            raise BackendError("timeout waiting for generation")

        if q.error:
            # HTTP 头已经发出去了，只能用 SSE 里的错误对象收尾：先报错，再按协议
            # 正常结束这个流（clients 普遍是看到 [DONE] 才算读完）。
            # 被拒（上下文装不下）时类型是客户端错误：流式下虽然已经回不了 400 状态码，
            # 但 type 字段至少要如实说这是请求的问题，客户端才好决定是重试还是改请求。
            self._record_perf(q)        # 失败也记账：被拒/出错本身是 A/B 要看的指标
            sse({"error": {"message": q.error,
                           "type": "invalid_request_error" if q.rejected
                                   else "server_error"}})
            self._chunk(b"data: [DONE]\n\n")
            self._chunk(b"")
            raise BackendError(q.error, soft=q.rejected)

        # 收尾前先把 stripper 里还攒着的字节放出来（正常情况是空；被截断在 <think> 里时
        # 会连标签一起吐出来，见 flush() 的说明），否则客户端会少一段正文。走 emit 而不是
        # 直接发：这段尾巴里也可能装着一个工具调用。
        if stripper is not None:
            tail = stripper.flush()
            if tail:
                emit(tail)

        # 同理：extractor 里攒着的可能是半个 <tool_call>（被 max_tokens 截断），
        # flush 会连标签一起交回来当正文，不吞。
        leftover, _ = extractor.flush()
        if leftover:
            sse(delta_obj(leftover))
            sent[0] += 1

        # 有工具调用时 finish_reason 必须是 tool_calls（理由同非流式路径）
        sse(delta_obj(None, finish="tool_calls" if n_calls[0]
                      else (q.finish_reason or "stop")))
        # 先算好这一轮的 perf 并**记账**，再发 [DONE]：客户端看到流结束的那一刻，
        # /v1/perf 的窗口里就已经有这一条了（基准脚本 reset→跑→读 才不会少一条）。
        perf = self._perf(q)
        if include_usage:
            sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": MODEL_ID, "choices": [],
                 "usage": self._usage(q),
                 # 非标准扩展：这一轮的 prefill/decode/TTFT/排队明细。
                 # 客户端不读它也没有任何影响；基准脚本与 /v1/perf 读它。
                 "perf": perf})
        self._record_perf(q, perf)
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")                # 终止 chunk
        self.gateway.log("stream done: rid=%d session=%d chunks=%d tool_calls=%d tokens=%d",
                         q.rid, q.session, sent[0], n_calls[0], q.decode_tokens)

    def _blocking_response(self, req, q, enable_thinking=None):
        q.done.wait(REQUEST_TIMEOUT)
        if not q.done.is_set():
            raise BackendError("timeout waiting for generation")
        if q.error:
            raise BackendError(q.error, soft=q.rejected)
        created = int(time.time())
        content, calls = split_reply(q, enable_thinking)
        message = {"role": "assistant", "content": content}
        finish = q.finish_reason or "stop"
        if calls:
            # 有工具调用时 finish_reason 必须是 tool_calls：客户端（以及各家 Agent 框架）
            # 就是靠它决定"该执行工具了"还是"这轮结束了"。照搬后端的 stop 会让 Agent
            # 以为模型已经答完，工具根本不会被调用。
            message["tool_calls"] = to_openai_tool_calls(calls)
            finish = "tool_calls"
        perf = self._perf(q)
        self._record_perf(q, perf)      # 先记账再回响应（同流式路径的理由）
        self._json(200, {
            "id": "chatcmpl-%d-%d" % (created, q.rid),
            "object": "chat.completion",
            "created": created,
            "model": MODEL_ID,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": self._usage(q),
            # 非标准扩展：本轮 prefill/decode/TTFT/排队明细（同流式路径）。
            "perf": perf,
        }, extra_headers=kv_reuse_headers(q.kv))

    def _usage(self, q):
        """OpenAI 口径的 usage。

        `prompt_tokens` 是**客户端发来的那段 prompt 的长度**，不是后端本轮真正算过
        的那部分。命中 KV 复用时后端只报增量（板上实测：整段 522 字节的 prompt，
        本轮只 prefill 了对应 84 字节的那部分；DONE 帧第 4 字段就是这个增量），
        照搬进 prompt_tokens 会让客户端以为上下文只有 21 tok——按它做上下文预算
        就**永远不会触发裁剪**，一路涨到后端自动清 KV，然后出现那种"只错一轮、
        不留痕"的答案。

        所以：prompt_tokens = 本轮结束时的上下文长度 − 本轮生成的 token 数；
        被复用的那部分放 prompt_tokens_details.cached_tokens（OpenAI 同名字段）。
        两个数都来自 DONE 帧（第 4 与第 8 字段），网关不需要自己分词。
        """
        ctx = q.context_tokens or (q.prefill_tokens + q.decode_tokens)
        prompt_tokens = max(0, ctx - q.decode_tokens)
        cached = max(0, prompt_tokens - q.prefill_tokens)
        return {"prompt_tokens": prompt_tokens,
                "completion_tokens": q.decode_tokens,
                "total_tokens": prompt_tokens + q.decode_tokens,
                # 恒存在（没错时是 0）：客户端读它时不必先判有没有这个键。
                "prompt_tokens_details": {"cached_tokens": cached}}

    def _perf(self, q):
        """一次请求的性能记录——**调度算法 A/B 的统一尺子**。

        三组字段，全部来自 DONE 帧与网关自己的单调钟，不改动任何请求语义：
          · 时延：ttft_ms / e2e_ms / queue_wait_s
          · 速率：prefill_tok_s / decode_tok_s / tpot_ms（真实 token 数 ÷ 阶段耗时）
          · 复用：prompt/cached/ctx 与 KV 的 sent/base/full 字节
        """
        kv = q.kv or {}
        pt, dt = q.prefill_tokens, q.decode_tokens
        pm, dm = q.prefill_ms, q.decode_ms
        ctx = q.context_tokens or (pt + dt)
        prompt_tokens = max(0, ctx - dt)
        cached = max(0, prompt_tokens - pt)
        return {
            "rid": q.rid, "session": q.session,
            "ok": (q.error is None and not q.rejected),
            "finish": q.finish_reason, "error": q.error, "rejected": q.rejected,
            "queue_wait_s": kv.get("wait"),
            "ttft_ms": q.ttft_ms(), "e2e_ms": q.e2e_ms(),
            "prefill_tokens": pt, "prefill_ms": round(pm, 3),
            "prefill_tok_s": round(pt / (pm / 1000.0), 2) if pm > 0 else None,
            "decode_tokens": dt, "decode_ms": round(dm, 3),
            "decode_tok_s": round(dt / (dm / 1000.0), 2) if dm > 0 else None,
            "tpot_ms": round(dm / dt, 3) if dt > 0 else None,
            "prompt_tokens": prompt_tokens, "cached_tokens": cached,
            "ctx_tokens": ctx,
            "reset": kv.get("reset"), "sent_bytes": kv.get("sent"),
            "base_bytes": kv.get("base"), "full_bytes": kv.get("full"),
            "t_submit": q.t_submit, "t_done": q.t_done,
        }

    def _record_perf(self, q, rec=None):
        """把一条记录放进滚动窗口，并打一行 JSON 进日志（`grep '\\[perf\\]'` 可离线分析）。

        调用时机有讲究：**必须在给客户端发 [DONE]/响应体之前**记账。否则客户端
        一看到流结束就查 `/v1/perf`，窗口里还没有这一条——基准脚本会读到少一条的
        聚合值，而且越短的请求越容易踩到。

        try 全包：统计是旁路，**任何统计异常都不允许影响请求本身**。
        """
        try:
            if rec is None:
                rec = self._perf(q)
            self.gateway.perf.add(rec)
            self.gateway.log("[perf] %s",
                             json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            return rec
        except Exception as exc:                            # noqa: BLE001
            self.gateway.log("perf record failed: %r", exc)
            return None


# 帧协议里 max_new_tokens 字段实际按 `int` 用（后端拿 `%llu` 读进来之后要落到
# `PendingInput::max_new_tokens` 这个 int）。后端 2026-09-16 起会**拒收**超出这个范围的
# 值（回 ERR），所以这里必须先卡住——否则一个客户端随口写 `"max_tokens": 1e12`
# 就能让后端回 ERR，而 ERR 的含义是"这个会话的驱动线程没了"，网关据此把会话标死：
# **四次离谱的 max_tokens 就能把整个服务打死**。这是客户端不该有能力做到的事。
MAX_NEW_TOKENS_CAP = 0x7fffffff


def _pick_max_tokens(req):
    for name in ("max_tokens", "max_completion_tokens", "n_predict", "max_new_tokens"):
        v = req.get(name)
        if isinstance(v, int) and v > 0:
            # **夹住而不是回 400**：`"max_tokens": 100000` 是客户端很常见的写法，意思是
            # "能吐多少吐多少"，不是"我要十万个 token"。真按 400 拒掉会把正常的 OpenAI
            # 风格客户端挡在门外。夹到上限之后语义仍然正确——后端本来就会在 EOS 或上下文
            # 耗尽时停，这个字段只是个上界。
            return min(v, MAX_NEW_TOKENS_CAP)
    return 0        # 0 = 让后端用它自己的默认值（--n-predict）


def _pick_tools(req):
    """OpenAI 的 `tools` / `tool_choice` -> 要注入 prompt 的工具列表（空 = 不注入）。

    注入方式是把工具定义写进 **system 消息**（Qwen3.5 模板就是这么定的），所以工具变了
    system 就变了，粘性复用的前缀判据会判"对不上"、这一轮全量重算。这没法避免——
    工具集本来就是这个对话的一部分。要注意的是**别让它每轮都变**：Agent 每轮传同一份
    tools，渲染出的字节就是同一份，复用照常。真正会踩的坑是客户端给 tools 里的
    dict 换了键顺序——渲染时键是排序的（toolcalls.jinja_tojson），顺序无关，没事。

    `tool_choice`：`"none"` 不注入；指定某个 function 就只注入它；其余（含
    `"auto"` / `"required"`）全注入。"required" 我们没法强制模型一定调用，就不假装
    能做到——把它当 auto 处理，比回一个 400 更接近客户端的意图。
    """
    raw = req.get("tools")
    if not isinstance(raw, list):
        return []
    tools = [t for t in raw if isinstance(t, dict)]
    if not tools:
        return []
    choice = req.get("tool_choice")
    if isinstance(choice, str) and choice.strip().lower() == "none":
        return []
    if isinstance(choice, dict):
        fn = choice.get("function")
        if not isinstance(fn, dict):
            fn = choice
        want = fn.get("name")
        if want:
            picked = [t for t in tools
                      if (t.get("function") or {}).get("name") == want]
            if picked:
                return picked
    return tools


class ThinkStripper(object):
    """把开头的 <think>…</think> 从正文里摘掉。**只在关思考时**使用。

    为什么需要：关思考是靠 /no_think 软开关实现的，模型照样会吐一个 `<think>` 段
    （实测通常是空的：`<think>  </think>  收到`）。原样返回的话 Agent 拿到的是带标签的
    字符串，还得自己洗一遍。这里在网关侧洗掉，两种响应形态用同一份逻辑。

    为什么开思考时不摘：那时的 <think> 段是**有内容的推理过程**，属于模型输出的一部分，
    擅自丢掉等于替用户做决定。要暴露的话应该另开一个字段（reasoning_content），
    这一版没做——所以开思考时原样透传，行为与不加这段代码时完全一致。

    流式为什么要缓冲：`</think>` 可能被切在两个 DELTA 帧之间（本来就是按 token 投递的），
    不能逐帧正则。于是维持一个小状态机，只在"还可能是在开头那个 think 段"的极短窗口里
    攒字节；一旦判定不是 think 段就立刻透传，不等。关思考时那段是空的，所以这个窗口
    通常只有几个 token 的延迟。
    """

    MARK_OPEN = "<think>"
    MARK_CLOSE = "</think>"
    MAX_HOLD = 1 << 16      # 攒这么多还没看到 </think> 就放弃摘除（见 body 分支）

    def __init__(self):
        # lead: 还在对 <think> 的开头 -> pre: 吃掉紧跟的空白 -> body: 找 </think>
        # -> post: 吃掉紧跟的空白 -> done: 剩下的一律透传
        self.state = "lead"
        self.buf = ""

    def feed(self, text):
        """送进一段（可能是半个字的）新文本，返回现在可以确定发出的部分。"""
        if not text:
            return ""
        self.buf += text
        out = []
        while self.buf:
            if self.state == "done":
                out.append(self.buf)
                self.buf = ""
                break
            if self.state == "lead":
                if self.buf.startswith(self.MARK_OPEN):
                    self.buf = self.buf[len(self.MARK_OPEN):]
                    self.state = "pre"
                    continue
                if self.MARK_OPEN.startswith(self.buf):
                    break                   # 还可能是 think 的开头：再等等
                self.state = "done"         # 不是 think 段：整段透传
                continue
            if self.state in ("pre", "post"):
                # 这两个状态是"吃掉紧随标签的空白"。必须做成**状态**而不是一次性
                # lstrip：按 token 投递时，标签和它后面的空格/换行常常落在两帧里，
                # 一次性 lstrip 那一刻缓冲区可能还是空的，空白就漏进正文了（踩过）。
                stripped = self.buf.lstrip()
                if not stripped:
                    self.buf = ""           # 全是空白：吃掉，继续等
                    break
                self.buf = stripped
                self.state = "body" if self.state == "pre" else "done"
                continue
            if self.state == "body":
                i = self.buf.find(self.MARK_CLOSE)
                if i < 0:
                    if len(self.buf) > self.MAX_HOLD:
                        # 关思考却吐了一大段推理（不该发生）。放弃摘除、原样透传：
                        # 宁可让用户看见标签，也不能把内存吃光。
                        self.state = "done"
                        out.append(self.MARK_OPEN + self.buf)
                        self.buf = ""
                        break
                    break                   # 推理段还没结束（正常情况下它是空的）
                self.buf = self.buf[i + len(self.MARK_CLOSE):]
                self.state = "post"
                continue
        return "".join(out)

    def flush(self):
        """收尾：把还攒着的字节交出来。

        卡在 pre/body（模型吐了 <think> 却没闭合，通常是 max_tokens 截断）时，把原始
        内容连同标签一起交出去，而不是丢弃——宁可让用户看见半个推理段，也不能把可能
        存在的正文吞掉。
        """
        out, self.buf = self.buf, ""
        if self.state in ("pre", "body"):    # 标签已经吃掉了，交还时补回去
            out = self.MARK_OPEN + out
        self.state = "done"
        return out


def strip_think(text, enable_thinking):
    """非流式路径：一次性把开头的 think 段摘掉。"""
    if enable_thinking is not False:
        return text
    s = ThinkStripper()
    return s.feed(text) + s.flush()


def reply_reasoning(enable_thinking):
    """扫工具调用时要不要跳过 `<think>` 段。

    开思考（默认，含客户端没明说）时要跳过：模型是"先推理、后调用"，但推理过程里
    **讨论**到 `<tool_call>` 字样并不等于它打算调用那个工具，抽出来执行就成了替模型
    做决定。关思考时不必——ThinkStripper 已经把开头那个（空的）think 段摘掉了。
    """
    return enable_thinking is not False


def split_reply(q, enable_thinking):
    """模型这一轮的回复 -> (给客户端的正文, 工具调用列表)。"""
    return split_tool_calls(strip_think(q.text(), enable_thinking),
                            reasoning=reply_reasoning(enable_thinking))


def assistant_continuation(q, enable_thinking):
    """这一轮的助手回复在**下一轮 prompt 里**长什么样（不含 assistant 头与 `<|im_end|>`）。

    响应路径和 KV 记账路径共用这一个函数，两边的字节因此天然一致——不一致的话前缀
    判据会失败、每轮全量重算，而那是"只慢不错"的静默退化。
    没有工具调用时它就是 `strip_think(...)` 本身（render_assistant_turn 的空列表分支
    原样返回正文），所以不带工具的老行为一个字节都没变。
    """
    content, calls = split_reply(q, enable_thinking)
    return render_assistant_turn(content, calls)


def kv_reuse_headers(kv):
    """把本轮的复用决策回报给客户端。

    为什么值得占一个响应头：**光看 usage.prompt_tokens 分不清「复用生效」和「换了个
    空会话」**——两种情况下 prefill 都可能等于全量。这里直接把会话号、是否 RESET、
    实际发出去多少字节摊开，出问题时不用猜。（也方便 Agent 侧观察缓存命中率。）

    `wait` 是**排队等了多久**（秒，0 = 没等）。它不是锦上添花：客户端看到的"慢"往往是
    这个数，而它此前在报文里**完全隐形**——页面上只看到 `1.57 tok/s`，看起来像模型慢，
    实际是四个会话都闲着、请求在队列里干等（2026-09-18 用户就是这么报上来的）。
    分母里带着它，速率就不是"解码速度"了，所以必须能单独看。
    """
    if not kv:
        return {}
    return {"X-KV-Reuse": "session=%d; reset=%d; sent=%d; base=%d; full=%d; swapped=%d; wait=%.1f"
                          % (kv["session"], 1 if kv["reset"] else 0, kv["sent"],
                             kv["base"], kv["full"], 1 if kv.get("swapped") else 0,
                             kv.get("wait", 0.0))}


# ===========================================================================
# 5. 自检：只验帧协议，不起 HTTP
# ===========================================================================

def selftest_reject(backend, check):
    """REJECT 的回归：后端拒了一轮（上下文装不下），网关这半边该做什么。

    对应的缺陷：serve 模式下"上下文将满"原本是后端**自动清 KV 再把这一轮跑完**，而
    网关只发增量（`prompt[len(base):]`）→ 模型拿到的是一段没有开头的对话 → 以一个
    自信的错答案、finish_reason=stop 返回。不报错、不重试、只错一轮，是最难查的一类。

    改成后端发 CLEAR+REJECT 之后，网关这半边有三条义务：
      ① 只失败这一条请求，而且是**软错误**（HTTP 400，而不是 503——客户端要做的是改
         请求，原样重试必然再被拒一次）；
      ② 会话照常留在池子里（标死等于每撞一次超限就永久少一个会话，四次之后没会话可用）；
      ③ 粘性记录作废（下一轮 RESET 全量重发），**且不能把这一轮的 prompt 记进 KV 账**
         ——这一轮一个 token 都没 prefill 进去。

    单独成一个函数是因为它要**另起一个桩后端**（--ctx-limit 是桩专有的开关）。
    真后端上这段跑不了，调用处会 SKIP。
    """
    rb = Backend(list(backend.argv) + ["--ctx-limit", "500"],
                 backend.log_path + ".reject", frame_fd=backend.frame_fd)
    rb.start()
    try:
        rpool = SessionPool(rb, idle_ttl=0, queue_timeout=5)
        # 第一轮：ctx 还是 0，不该触发（真后端同理：空的会话没什么"装不下"的）
        l1 = rpool.acquire("reject-probe", render_messages(
            [{"role": "user", "content": "记住暗号：青柠味苏打水。"}]))
        q1 = rb.submit(l1.sent_prompt, 8, session=l1.session, reset=l1.reset)
        rb.wait(q1)
        rpool.release(l1, q1.text(), q1.was_cleared)
        sess = l1.session
        rb.release_request(q1)
        check("第一轮正常跑完（还没到上限）", q1.error is None and q1.decode_tokens > 0,
              "ctx=%d" % q1.context_tokens)

        # 第二轮：桩的 ctx 已经攒够，会回 CLEAR + REJECT
        l2 = rpool.acquire("reject-probe", render_messages(
            [{"role": "user", "content": "记住暗号：青柠味苏打水。"},
             {"role": "assistant", "content": q1.text()},
             {"role": "user", "content": "再说一遍暗号。"}]))
        q2 = rb.submit(l2.sent_prompt, 8, session=l2.session, reset=l2.reset)
        soft = None
        try:
            rb.wait(q2)
        except BackendError as exc:
            soft = exc
        check("被拒的那轮抛的是**软**错误（HTTP 层会回 400 而不是 503）",
              soft is not None and soft.soft and "context limit" in str(soft), "%r" % (soft,))
        check("这一轮确实没跑（没有 DONE 的计数）",
              q2.decode_tokens == 0 and q2.finish_reason is None)
        check("CLEAR 帧到了（粘性记录必须作废）", q2.was_cleared)

        # 模拟 HTTP 线程的 finally 那段处置（这里没有 HTTP 层，selftest 只到协议这一层）
        if q2.rejected:
            rpool.discard(l2)
        else:                                   # pragma: no cover - 上面的断言已覆盖
            rpool.release(l2, "", True)
        rb.release_request(q2)
        check("会话没被标死（还能继续用）", not rpool.dead[sess], "dead=%s" % (rpool.dead[sess],))
        check("会话已归还（不再占着租约）", not rpool.busy[sess])
        check("KV 账没被这一轮污染：known 是 None（未知），不是本轮 prompt",
              rpool.known[sess] is None, "known=%r" % (rpool.known[sess],))

        # 下一轮必须 RESET 全量重发（known=None 的语义），而不是拿增量去拼一段残缺历史
        l3 = rpool.acquire("reject-probe", render_messages(
            [{"role": "user", "content": "全新的一轮。"}]))
        check("被拒之后下一轮走 RESET 全量重发，不复用", l3.reset and l3.base == "",
              "reset=%s base=%r" % (l3.reset, l3.base[:20]))
        rpool.release(l3, "", True)
    finally:
        rb.stop()


class _AliveOnlyBackend(object):
    """只给 selftest_pool_liveness 用：池子对后端只要求 `sessions` / `on_clear` / `alive` 三样。

    这里敢用桩是因为被测的是**池子那条判据本身**（"等不出结果就别让人排"），它与帧协议、
    与哪台后端都无关；用真后端反而要额外起一个进程、白等 240s 加载模型。端到端那一半
    （真后端被打死之后，5 段对话不再一起空等到 600s）在板卡上验，见文档 §9.11。
    """

    def __init__(self, sessions=4):
        self.sessions = sessions
        self.on_clear = None
        self.is_alive = True

    def alive(self):
        return self.is_alive


def selftest_pool_liveness(check):
    """后端没了 / 会话全 dead：`acquire` 必须**立刻**报错，而不是让人排在队列里空等。

    对应的现场（2026-09-17 上板实测）：一个 3.4 MB 的请求让后端进程退出（见 main.cc 的
    kServeMaxPromptBytes 分支），四个会话全 dead；之后 4 个终端 + 1 个 Claude Code 一起
    进来排队，屏幕上什么都不打印，600s 之后才各收到一个 503。看相是"网关卡了"，真相是
    队列在等一个已经不存在的东西——所以这条判据必须在**进队列之前**生效。

    判据本身也有"写松了"的两种形态，两条反面对照就钉它们：
      · 写成恒失败 —— 后端活着、会话有空时必须照常拿到会话；
      · 两种情况糊成一句话 —— 错误信息要分得清"后端死了"还是"会话没了"，现场靠它
        决定下一步（前者只能重启网关，后者同样是，但成因完全不同）。
    """
    be = _AliveOnlyBackend(4)
    pool = SessionPool(be, idle_ttl=0, queue_timeout=5)

    def timed_acquire(key):
        t0 = time.time()
        try:
            pool.acquire(key, "hi")
            return None, time.time() - t0
        except BackendError as exc:
            return exc, time.time() - t0

    lease = pool.acquire("live", "hi")
    pool.release(lease, "hi", False)

    # ① 后端子进程没了
    be.is_alive = False
    err, dt = timed_acquire("backend-gone")
    check("后端没了 => 立刻报错，不在队列里空等到 queue_timeout",
          err is not None and not err.soft and dt < 1.0, "%.2fs %r" % (dt, err))
    check("错误分得清是「后端退出了」", err is not None and "backend" in str(err),
          "%r" % (err,))

    # ② 后端活着，但四个会话全 dead（dead 没有任何复活路径）
    be.is_alive = True
    for s in range(pool.n):
        pool.mark_dead(s)
    err, dt = timed_acquire("all-dead")
    check("四个会话全 dead => 立刻报错，不在队列里空等到 queue_timeout",
          err is not None and not err.soft and dt < 1.0, "%.2fs %r" % (dt, err))
    check("错误分得清是「会话没了」", err is not None and "dead" in str(err), "%r" % (err,))


def selftest_continue(check):
    """「继续回答」那一半（acquire_continue + 记账），不碰帧协议。

    半截答案得留在 KV 账上、继续时不 RESET 不发新 prompt、收尾把「半截 + 续写」拼回 known。
    任何一环写错，下一轮前缀判据就静默全废（那正是这套记账存在的意义反面）。用
    `_AliveOnlyBackend` 是因为被测的是池子这条判据本身，与后端、与帧协议无关。
    """
    be = _AliveOnlyBackend(4)
    pool = SessionPool(be, idle_ttl=0, queue_timeout=5)
    key = "continue-probe"
    prompt = render_messages([{"role": "user", "content": "hi"}])

    l1 = pool.acquire(key, prompt)
    s = l1.session
    pool.release(l1, "partial", False)
    check("第一轮之后 known = prompt + 半截", pool.known[s] == prompt + "partial",
          repr(pool.known[s]))

    l2 = pool.acquire_continue(key)
    check("continue 拿到**同一个**会话", l2.session == s, "s%d vs s%d" % (l2.session, s))
    check("continue 不 RESET、不发新 prompt", not l2.reset and l2.sent_prompt == "",
          "reset=%s sent=%r" % (l2.reset, l2.sent_prompt))
    check("continue 的 base 是「prompt + 半截」（记账要它）",
          l2.base == prompt + "partial", repr(l2.base))

    pool.release(l2, "cont", False)
    check("继续之后 known = prompt + 半截 + 续写",
          pool.known[s] == prompt + "partial" + "cont", repr(pool.known[s]))

    # 反例：根本没有这段对话 => 明确报错，而不是静默跑一条空 RESUME。
    try:
        pool.acquire_continue("nobody")
        check("continue 一段不存在的对话该报错", False, "没报错")
    except BackendError as exc:
        check("continue 一段不存在的对话该报错（说清原因）",
              "no session" in str(exc) or "reaped" in str(exc), repr(exc))


def _fixture(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def selftest_system(check, backend=None):
    """资源观测那半边：/proc 解析 + CPU 百分比算术 + STAT 帧往返。

    为什么非要夹具目录：`HostSampler` 在**本机（Windows）根本没有 /proc**，只按真板卡
    验的话，这段解析和百分比算术要等到上了板子、盯着页面看才发现算错。`root=` 就是
    为这个留的——把 /proc、/sys 换成一个临时目录，本机就能把**算术**真跑一遍。

    CPU 占用率是两次采样之差，夹具就靠**换 jiffies** 造出确定的一段：百分比只跟差值
    有关，跟两次采样之间真实的墙钟无关，所以这里的期望值是精确的、不是"大约"。
    """
    root = tempfile.mkdtemp(prefix="sysfix-")
    try:
        # ---- /proc/stat：四个核各钉一件事 ----
        # cpu0 差值 100/100 = 100.0%（满核）；cpu1 差 0/100 = 0.0%（空核）；
        # cpu2 **这一段只涨 iowait**（iowait 必须算空闲，否则等磁盘会被报成 CPU 忙）
        #  ⚠️ 涨的必须是 iowait 那一格、不能是 idle 格：只动 idle 的话"把 iowait 当忙"
        #  这个错法在**两次采样的差值**上正好抵消（两个快照都多算了那 50），变异体
        #  会活下来（第一版就是这样，M1 逃掉了）。
        # cpu3 差值**为负**（时钟回摆/计数器重置那种形态）必须夹到 0.0，不能吐负数。
        _fixture(root, "proc/stat", """\
cpu  1000 0 200 7000 0 0 0 0 0 0
cpu0 400 0 100 3000 0 0 0 0 0 0
cpu1 300 0 50 2000 0 0 0 0 0 0
cpu2 200 0 25 1500 50 0 0 0 0 0
cpu3 100 0 25 500 0 0 0 0 0 0
intr 0
""")
        # MemAvailable 6/16 GB ⇒ 用掉 62.5%。若误用 MemFree（2 GB）会算成 87.5%，
        # 所以这个期望值同时钉住了"取的是哪一格"。
        _fixture(root, "proc/meminfo", """\
MemTotal:       16000000 kB
MemFree:         2000000 kB
MemAvailable:    6000000 kB
Buffers:          100000 kB
Cached:          3000000 kB
SReclaimable:     200000 kB
SwapTotal:       4194300 kB
SwapFree:        4194300 kB
""")
        _fixture(root, "proc/loadavg", "0.50 0.40 0.35 2/1234 4567\n")
        _fixture(root, "proc/uptime", "12345.67 12345.67\n")
        # 温度：三个好区 + 一个 temp 是垃圾 + 一个连 type 都没有。后两个必须被跳过
        # 而不是让整段报错（真机上某个区偶尔读不出来不能拖垮整个面板）。
        _fixture(root, "sys/class/thermal/cooling_device0/type", "processor\n")
        _fixture(root, "sys/class/thermal/thermal_zone0/type", "soc-thermal\n")
        _fixture(root, "sys/class/thermal/thermal_zone0/temp", "45000\n")
        _fixture(root, "sys/class/thermal/thermal_zone1/type", "bigcore0-thermal\n")
        _fixture(root, "sys/class/thermal/thermal_zone1/temp", "51234\n")
        _fixture(root, "sys/class/thermal/thermal_zone2/type", "npu-thermal\n")
        _fixture(root, "sys/class/thermal/thermal_zone2/temp", "48000\n")
        _fixture(root, "sys/class/thermal/thermal_zone3/type", "gpu-thermal\n")
        _fixture(root, "sys/class/thermal/thermal_zone3/temp", "not-a-number\n")
        _fixture(root, "sys/class/thermal/thermal_zone4/type", "missing-temp\n")

        print("== HostSampler：第一次采样只该有「静态量」，百分比必须是 None 而不是 0 ==")
        hs = HostSampler(root=root)
        a = hs.sample()
        check("核数认得出（cpu0..cpu3）", a["cpu"]["n"] == 4, "n=%r" % (a["cpu"]["n"],))
        check("第一次 CPU%% 是 None（没上一次可减，不是 0%）",
              a["cpu"]["pct"] is None, "= %r" % (a["cpu"]["pct"],))
        check("第一次每核也都是 None",
              a["cpu"]["per_core"] == [None] * 4, "= %r" % (a["cpu"]["per_core"],))
        check("静态量第一次就有", a["mem"]["used_pct"] == 62.5 and len(a["thermal"]) == 3,
              "used_pct=%r 热区=%d 个" % (a["mem"]["used_pct"], len(a["thermal"])))
        check("loadavg 三个数", a["cpu"]["loadavg"] == [0.5, 0.4, 0.35],
              "= %r" % (a["cpu"]["loadavg"],))
        check("uptime 保留一位", a["uptime_s"] == 12345.7, "= %r" % (a["uptime_s"],))

        # ---- 第二次采样：换一份 jiffies，差值就是上面设计好的那几组 ----
        # 注意每一行都要自己算一遍：百分比只跟**差值**有关，写错一格就会得到一个
        # "看着挺合理"的数（第一版把 cpu 行的 idle 写成了 7975，于是 25/100 变成了
        # 2.5/100，自检报 FAIL —— 夹具写错和代码写错在这里是同一个现象）。
        _fixture(root, "proc/stat", """\
cpu  1025 0 200 7075 0 0 0 0 0 0
cpu0 500 0 100 3000 0 0 0 0 0 0
cpu1 300 0 50 2100 0 0 0 0 0 0
cpu2 200 0 25 1500 150 0 0 0 0 0
cpu3 90 0 20 600 0 0 0 0 0 0
intr 0
""")
        b = hs.sample()
        print("== 第二次采样：CPU 百分比 ==")
        check("整机 25/100 => 25.0%", b["cpu"]["pct"] == 25.0, "= %r" % (b["cpu"]["pct"],))
        check("cpu0 满核 => 100.0%", b["cpu"]["per_core"][0] == 100.0,
              "= %r" % (b["cpu"]["per_core"][0],))
        check("cpu1 差值为 0 => 0.0%", b["cpu"]["per_core"][1] == 0.0,
              "= %r" % (b["cpu"]["per_core"][1],))
        check("cpu2 只涨 iowait => 0.0%（等磁盘不算 CPU 在干活）",
              b["cpu"]["per_core"][2] == 0.0, "= %r" % (b["cpu"]["per_core"][2],))
        check("cpu3 差值为负 => 夹到 0.0%（不吐负数）",
              b["cpu"]["per_core"][3] == 0.0, "= %r" % (b["cpu"]["per_core"][3],))
        check("热区按 zone 号排序、label 与摄氏度都对",
              [(t["zone"], t["label"], t["c"]) for t in b["thermal"]] ==
              [("thermal_zone0", "soc-thermal", 45.0),
               ("thermal_zone1", "bigcore0-thermal", 51.2),
               ("thermal_zone2", "npu-thermal", 48.0)],
              "%r" % (b["thermal"],))

        print("== 读不到就退化，不抛（观测端点不该因为一个文件读不到就 500）==")
        empty = HostSampler(root=os.path.join(root, "does-not-exist")).sample()
        check("没有 /proc 也不抛，核数是 None（**不是 0**——0 核是假话）",
              empty["cpu"]["n"] is None and empty["cpu"]["pct"] is None,
              "n=%r pct=%r" % (empty["cpu"]["n"], empty["cpu"]["pct"]))
        check("内存退化成空而不是 0 占用",
              empty["mem"].get("total", 0) == 0 and "used_pct" not in empty["mem"],
              "total=%r" % (empty["mem"].get("total"),))
        check("热区退化成空表", empty["thermal"] == [], "= %r" % (empty["thermal"],))
        check("uptime 退化成 None", empty["uptime_s"] is None, "= %r" % (empty["uptime_s"],))

        print("== CardStats：Δ忙时 / Δ墙钟 ==")
        def card(name, busy_us, mem_at_us, **kw):
            d = {"name": name, "busy_us": busy_us, "run_calls": 7, "mem_at_us": mem_at_us,
                 "mem_total": 1000, "mem_free": 400, "node_num": 8,
                 "node_min_free": 50, "ctx_len": 8192}
            d.update(kw)
            return d

        cs = CardStats()
        first = cs.sample({"now_us": 1000000,
                           "cards": [card("stage0", 100000, 900000),
                                     card("stage1", 100000, 0)]})
        check("第一次 busy_pct 是 None（不是 0%）",
              first["busy_pct"] is None and
              all(c["busy_pct"] is None for c in first["cards"]),
              "= %r" % (first["busy_pct"],))
        check("整卡占用 = (total-free)/total", first["cards"][0]["mem_used"] == 600 and
              first["cards"][0]["mem_used_pct"] == 60.0,
              "%r/%r" % (first["cards"][0]["mem_used"], first["cards"][0]["mem_used_pct"]))
        check("mem_age_s 按后端自己的时钟算（0.1s）",
              first["cards"][0]["mem_age_s"] == 0.1,
              "= %r" % (first["cards"][0]["mem_age_s"],))
        check("mem_at_us==0 => mem_age_s 是 None（一次都没采到，不是「很旧」）",
              first["cards"][1]["mem_age_s"] is None,
              "= %r" % (first["cards"][1]["mem_age_s"],))

        second = cs.sample({"now_us": 2000000,           # 距上一次 1.0s
                            "cards": [card("stage0", 600000, 1900000),
                                      card("stage1", 1100000, 1900000),
                                      card("stage2", 5, 1900000)]})
        check("stage0 Δ0.5s / 1.0s => 50.0%", second["cards"][0]["busy_pct"] == 50.0,
              "= %r" % (second["cards"][0]["busy_pct"],))
        check("stage1 Δ1.0s / 1.0s => 100.0%", second["cards"][1]["busy_pct"] == 100.0,
              "= %r" % (second["cards"][1]["busy_pct"],))
        check("上一帧没出现过的卡 => None（不拿 0 冒充基线）",
              second["cards"][2]["busy_pct"] is None,
              "= %r" % (second["cards"][2]["busy_pct"],))
        check("整机取的是**有值的那几张**的平均（不是拿 None 当 0 摊进去）",
              second["busy_pct"] == 75.0, "= %r" % (second["busy_pct"],))

        third = cs.sample({"now_us": 2500000,            # 距上一次只有 0.5s
                           "cards": [card("stage0", 1600000, 2400000),
                                     card("stage1", 1100000, 2400000)]})
        check("Δ忙时 > Δ墙钟（采样点被压近）=> 夹到 100.0%，不吐 200%",
              third["cards"][0]["busy_pct"] == 100.0,
              "= %r" % (third["cards"][0]["busy_pct"],))
        check("忙时倒退（不该发生但别崩）=> 该卡给 None、其余照算",
              cs.sample({"now_us": 3000000,
                         "cards": [card("stage0", 1, 2900000)]})["cards"][0]["busy_pct"]
              is None, "")

        # `now_us` 缺失/为 0 的 STAT（后端少填一个字段）：Δ墙钟 = 0 ⇒ 除零 ⇒
        # **整个 /v1/system 500**。这条不是"理论上的"——本轮的夹具正好撞出来过。
        cs0 = CardStats()
        z = [cs0.sample({"cards": [card("stage0", 0, 0)]}),
             cs0.sample({"cards": [card("stage0", 0, 0)]}),
             cs0.sample({"cards": [card("stage0", 500, 0)]})]
        check("后端没给 now_us（=0）连采三次：不抛除零，busy_pct 一律 None",
              [d["cards"][0]["busy_pct"] for d in z] == [None, None, None],
              "= %r" % ([d["cards"][0]["busy_pct"] for d in z],))

        # 同一份 doc 连着采两次：后端那边 now_us 没动（拿到的是缓存 / 答复超时）。
        # 这时候**不许**把窗口推过去、也不许把上一次的数抹成 "—"。
        # 板上就是这么闪的（2026-09-20）：busy_pct 在 0.0 和 None 之间来回跳。
        cs2 = CardStats()
        cs2.sample({"now_us": 1000000, "cards": [card("stage0", 0, 0)]})
        ready = cs2.sample({"now_us": 2000000, "cards": [card("stage0", 500000, 0)]})
        check("（前置）两次不同 doc 才算出 50.0%", ready["cards"][0]["busy_pct"] == 50.0,
              "= %r" % (ready["cards"][0]["busy_pct"],))
        again = cs2.sample({"now_us": 2000000, "cards": [card("stage0", 500000, 0)]})
        check("同一份 doc 再来一次：给回上一次的数，不是 None",
              again["cards"][0]["busy_pct"] == 50.0,
              "= %r" % (again["cards"][0]["busy_pct"],))
        # 关键：窗口没被这次"重复"吃掉——下一份真 doc 的 Δ 是从**上一次真 doc**起算的。
        # 若把 _prev 推到了那份旧 doc 上，这里会算成 100%（Δ0.5s / Δ0.5s）而不是 50%。
        span = cs2.sample({"now_us": 3000000, "cards": [card("stage0", 1000000, 0)]})
        check("重复采样不吃掉窗口：下一份 doc 仍从上一份**真** doc 起算（50%，不是 100%）",
              span["cards"][0]["busy_pct"] == 50.0,
              "= %r" % (span["cards"][0]["busy_pct"],))

        # ---- RK1828 结温：那个 root 采集服务写出来的文件 ----
        # 为什么这几条非要在本机夹具里钉：**本机永远读不到那个文件**（它只存在于板卡
        # 的 /run 下、还只有 root 那个采集进程会写）。没有夹具的话，"读不到画 — 而不是
        # 0"、"按 stage 序号对上 Device 序号"、"文件的年龄"这三件事全都只能等到上了板子、
        # 盯着页面看才发现——而 0°C 和 "—" 在屏幕上都是"没有内容"，最容易看漏的正是它。
        print("== RK1828 结温：rknn-smi 采集服务那个文件 ==")
        tfile = os.path.join(root, "rknn-temp.json")

        check("文件不存在 => 空表 + 没有时刻（不是 0 度）",
              _read_card_temps(tfile) == ({}, None),
              "= %r" % (_read_card_temps(tfile),))

        # 板上抓下来的形状：Device 2 那次没读到（`c` 是 null）。**它必须整条消失**，
        # 不能变成 0.0 —— 那是在屏幕上说"这张卡凉快"。
        t0 = time.time()
        _fixture(root, "rknn-temp.json", json.dumps({
            "ts": t0 - 3.0,
            "devices": [{"id": 0, "c": 28.0}, {"id": 1, "c": 35.5},
                        {"id": 2, "c": None}, {"id": 3, "c": 47.0},
                        # 下面这三条是"写文件的人变了/手改过"的形态，一律跳过不猜
                        {"id": "x", "c": 5}, {"c": 9}, "nonsense"],
        }))
        got_t, got_at = _read_card_temps(tfile)
        check("读得出来，且 null 那条不在表里（不变成 0.0）",
              got_t == {0: 28.0, 1: 35.5, 3: 47.0}, "= %r" % (got_t,))
        check("时刻读得出来", got_at is not None and abs(got_at - (t0 - 3.0)) < 0.5,
              "= %r" % (got_at,))

        # 坏文件（写了一半 / 手改坏了）=> 空表，绝不让 /v1/system 500
        _fixture(root, "rknn-temp.json", '{"ts": 1, "devices": [ ')
        check("半截 JSON => 空表，不抛", _read_card_temps(tfile) == ({}, None),
              "= %r" % (_read_card_temps(tfile),))
        _fixture(root, "rknn-temp.json", '[1, 2, 3]')
        check("顶层不是对象 => 空表", _read_card_temps(tfile) == ({}, None),
              "= %r" % (_read_card_temps(tfile),))

        # 现在把温度接到卡上。夹具里四张卡的名字就是真机那边的 stage0..3，
        # 但**故意乱序**放，钉住"按名字里的序号对、不是按列表位置对"。
        cs3 = CardStats()
        doc3 = {"now_us": 1000000,
                "cards": [card("stage2", 0, 0), card("stage0", 0, 0)]}
        cs3.sample(doc3)
        r3 = cs3.sample(doc3, {0: 28.0, 1: 35.5, 3: 47.0}, t0 - 3.0)
        check("温度按 stage 序号对上 Device 序号（不是按列表位置）",
              [c["temp_c"] for c in r3["cards"]] == [None, 28.0],
              "= %r" % ([c["temp_c"] for c in r3["cards"]],))
        check("文件的年龄只报一次（npu 那层），不是抄进每张卡",
              r3["temp_age_s"] is not None and 3.0 <= r3["temp_age_s"] < 4.0 and
              "temp_age_s" not in r3["cards"][0],
              "temp_age_s=%r card_keys=%s" % (r3["temp_age_s"], sorted(r3["cards"][0])))

        # 没有那个文件时：**三处都必须是 None**。任何一处漏成 0 都是"假读数"。
        r4 = cs3.sample(doc3)
        check("没有温度文件 => 每张卡 temp_c 是 None（不是 0）",
              [c["temp_c"] for c in r4["cards"]] == [None, None],
              "= %r" % ([c["temp_c"] for c in r4["cards"]],))
        check("没有温度文件 => temp_age_s 也是 None（不是 0，0 会说成「刚刚采的」）",
              r4["temp_age_s"] is None, "= %r" % (r4["temp_age_s"],))

        # 认不出 `stage<i>` 这种名字（改了 main.cc 的命名）就别猜，用列表位置兜底——
        # 猜错的温度比没有温度更坏：屏幕上没人看得出它对错了卡。
        r5 = CardStats().sample({"now_us": 1000000,
                                 "cards": [card("card_A", 0, 0), card("card_B", 0, 0)]},
                                {0: 11.0, 1: 22.0}, None)
        check("认不出 stage<i> 就按列表位置兜底（不猜成 None 也不瞎对）",
              [c["temp_c"] for c in r5["cards"]] == [11.0, 22.0],
              "= %r" % ([c["temp_c"] for c in r5["cards"]],))

        # 时钟往回跳（NTP 校时）时年龄会算成负数，夹成 0：屏幕上"-3.2s 前"没法解释。
        r6 = cs3.sample(doc3, {0: 28.0}, time.time() + 5)
        check("采样时刻在未来（NTP 回拨）=> 年龄夹到 0.0，不是负数",
              r6["temp_age_s"] == 0.0, "= %r" % (r6["temp_age_s"],))

        print("== /v1/system 的快照缓存：同一秒里后来的看客不许再采一次 ==")
        # 为什么这条重要到要单独验：一次快照 = 读十几个 procfs/sysfs 文件 + 一次 STAT
        # 往返，而这些都是**被看的那台机器**出的钱（四路推理就在同一颗 RK3588 上跑）。
        # 人数一多，"看状态"这件事本身就能把被观测的机器拖慢——这正是用户说的
        # "尽量减少这个展示功能对于硬件资源的占用"。
        calls = {"stats": 0, "host": 0}

        class _Be(object):
            """替身后端：它**只数采了几次**。这条要钉的正是次数，不是内容。"""

            sessions = 2
            ctx_size = 8192

            def __init__(self):
                self.stats = {"ok": False, "busy_pct": None,
                              "cards": [card("stage0", 0, 0)]}
                self.stats_at = None

            def request_stats(self, min_interval=0.0, wait=0.0):
                calls["stats"] += 1
                return self.stats

            def alive(self):
                return True

        class _Pool(object):
            def snapshot(self):
                return {"slots": [{"state": "busy"}], "waiting": []}

        class _Hs(HostSampler):
            def sample(self, *a, **kw):
                calls["host"] += 1
                return HostSampler.sample(self, *a, **kw)

        # 顺便把 `Gateway.temp_file` 这条线也接上：真机上它默认是 /run/rknn-temp.json，
        # 而构造函数里那一层**最容易漏**——参数收下了、`_system_snapshot_now` 里却忘了
        # 往外传，页面上就永远是 "—"，而单测 `_read_card_temps` 照样全绿。
        _fixture(root, "rknn-temp.json", json.dumps(
            {"ts": time.time(), "devices": [{"id": 0, "c": 28.0}]}))
        gw = Gateway(_Be(), _Pool(), temp_file=tfile)
        gw.host = _Hs(root=root)
        d1 = gw.system_snapshot()
        check("Gateway.temp_file 传到了卡上（不是只传了一半就丢）",
              d1["npu"]["cards"][0]["temp_c"] == 28.0 and
              d1["npu"]["temp_age_s"] is not None,
              "temp_c=%r age=%r" % (d1["npu"]["cards"][0].get("temp_c"),
                                    d1["npu"].get("temp_age_s")))
        d2 = gw.system_snapshot()
        check("同一秒里的第二次调用：STAT 与 /proc 都只采了那一次",
              calls["stats"] == 1 and calls["host"] == 1,
              "stats=%d host=%d" % (calls["stats"], calls["host"]))
        # **同一个对象**、不只是"内容一样"：内容一样也可能是又采了一遍然后恰好相同。
        check("第二个人拿到的是同一份快照（ts 也同一个）", d1 is d2,
              "%r vs %r" % (d1.get("ts"), d2.get("ts")))
        check("返回的那份是完整形状（host/npu/backend/gateway 都在）",
              all(k in d1 for k in ("ts", "host", "npu", "backend", "gateway")),
              "keys=%s" % sorted(d1.keys()))

        d3 = gw.system_snapshot(max_age=0)
        check("max_age=0 绕开缓存、真去重采一次",
              calls["stats"] == 2 and calls["host"] == 2 and d3 is not d1,
              "stats=%d host=%d" % (calls["stats"], calls["host"]))

        # 过期这一条是**必须有**的：只测"1 秒内不重采"的话，一个"缓存永不过期"的
        # 写法（忘了比时间，或者时间戳写成 0）在自检里照样全绿——而它在板上是
        # 资源条冻在打开那一帧、温度内存永远不动，屏幕上完全看不出是坏了。
        time.sleep(SYS_CACHE_S + 0.05)
        d4 = gw.system_snapshot()
        check("过了 %gs 之后再问要重采（缓存不许永不过期）" % SYS_CACHE_S,
              calls["stats"] == 3 and d4 is not d3,
              "stats=%d" % calls["stats"])
        check("重采出来的那份 ts 必须是新的（页面靠它判断要不要重画）",
              d4["ts"] > d1["ts"], "%r -> %r" % (d1["ts"], d4["ts"]))

        print("== 后端 STAT 帧的解包与缓存 ==")
        bad_input = CardStats().sample(None)
        check("后端还没答过（stats=None）=> ok=False，不抛",
              bad_input == {"ok": False, "cards": [], "busy_pct": None,
                            "temp_age_s": None},
              "= %r" % (bad_input,))

        be = Backend(["true"], os.path.join(root, "unused.log"), frame_fd="stdout")
        be._on_stats(b'{"now_us": 1, "cards": [{"name": "s", "busy_us": 2}]}')
        check("好载荷进缓存、序号 +1", be.stats_seq == 1 and
              be.stats["cards"][0]["name"] == "s", "seq=%d" % be.stats_seq)
        be._on_stats(b"{ this is not json")
        check("坏载荷只丢这一帧，读线程不许死（stats_seq 不动、stats 不脏）",
              be.stats_seq == 1, "seq=%d" % be.stats_seq)
        check("没起进程时 request_stats 返回缓存且不抛",
              be.request_stats(min_interval=0) is be.stats, "")
        try:
            got = be.request_stats(min_interval=0)
            raised = False
        except Exception:                                   # noqa: BLE001
            got, raised = None, True
        check("观测端点永不抛异常", not raised, "= %r" % (got,))

        if backend is not None and any("fake_backend" in str(a) for a in backend.argv):
            print("== STAT 帧真往返（桩后端；真正要钉住的是**帧流没被这帧撑错位**）==")
            seq0 = backend.stats_seq
            live = backend.request_stats(min_interval=0, wait=2.0)
            seq1 = backend.stats_seq
            ok_live = isinstance(live, dict) and live.get("cards")
            check("桩答得出 STAT", bool(ok_live),
                  "%d 张卡" % (len(live.get("cards") or [])) if ok_live else repr(live))
            if ok_live:
                live2 = backend.request_stats(min_interval=0, wait=2.0)
                seq2 = backend.stats_seq
                # 每要一次就恰好该多一条答复。载荷没被读走的话，读线程下一帧看到的
                # 是一段 JSON 当帧头，从此再也认不出 STAT ⇒ 序号就停在这儿了。
                # 判"两张卡数一样"是不够的：错位之后页面拿到的是**同一份缓存**，
                # 卡数一样、内容也一样旧。
                check("每要一次恰好多一条答复（载荷被完整消费，没留下半帧）",
                      seq1 == seq0 + 1 and seq2 == seq1 + 1,
                      "seq %d -> %d -> %d" % (seq0, seq1, seq2))
                check("第二次是**新**快照，不是把上一次的缓存还回来",
                      isinstance(live2, dict) and (live2.get("now_us") or 0) >
                      (live.get("now_us") or 0),
                      "now_us %r -> %r" % (live.get("now_us"), (live2 or {}).get("now_us")))
                c0 = (live.get("cards") or [{}])[0]
                check("每张卡该带来的字段都在",
                      all(k in c0 for k in ("name", "busy_us", "run_calls", "mem_at_us",
                                            "mem_total", "mem_free", "node_num",
                                            "node_min_free", "ctx_len")),
                      "keys=%s" % sorted(c0.keys()))
                # 上面几条都一样、但流已经错位一帧时，最先炸的是**下一个普通请求**。
                # 所以最后再跑一轮真问答：这帧要是把流撑歪了，这里必然不通过。
                q = backend.submit(render_messages([{"role": "user",
                                                     "content": "答一个字。"}]),
                                   16, session=0, reset=True)
                # `wait()` 成功时**返回 q**、失败时抛，所以别写成 `err = wait(q)` 再判
                # None——那永远是假的（第一版就这么写的，于是这一项无脑 FAIL）。
                try:
                    backend.wait(q, timeout=REQUEST_TIMEOUT)
                    err = None
                except BackendError as exc:
                    err = exc
                text = q.text()
                backend.release_request(q)
                check("STAT 之后普通请求照常（帧流对齐）",
                      err is None and len(text) > 0,
                      "%r / %d 字" % (err, len(text)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def selftest_cors(check):
    """跨源放行这条线：**默认一个 CORS 头都不发**，配了才按精确匹配回。

    为什么值得单独占一块：这段代码坏起来是"静默地把门打开"——多回一个
    `Access-Control-Allow-Origin: *` 之后，功能上**什么都不坏**（同源的演示页照样跑、
    别的测试照样全绿），只有站在"别人的浏览器"那个角度看才看得见。所以下面不只验
    cors_headers() 的返回值，还**真起一个 HTTP 服务、真读响应头**：四个发响应的地方
    是各写各的，函数返回值对不代表线路上发出去的头对。
    """
    print("== 跨源放行（CORS）：默认不放开，配了才按精确匹配回 ==")
    check("没配 --cors-origin：带 Origin 的请求也不发任何 CORS 头",
          cors_headers(None, "http://somebody-elses-page.example") == {}, "")
    check("没配时连 `*` 都不发（这条防线唯一要紧的性质）",
          "*" not in cors_headers(None, "http://x.example").values(), "")
    check("没带 Origin（curl / python 那种）：什么也不发（没有来源可回）",
          cors_headers("http://a.example", None) == {}, "")
    matched = cors_headers("http://a.example", "http://a.example")
    check("配了且精确匹配：回那个来源 + expose X-KV-Reuse / X-Request-Id",
          matched == {"Access-Control-Allow-Origin": "http://a.example",
                      "Access-Control-Expose-Headers": "X-KV-Reuse, X-Request-Id"},
          "= %r" % (matched,))
    check("配了但来源不匹配：一个头都不发（**不许退回 `*`**）",
          cors_headers("http://a.example", "http://b.example") == {}, "")
    # 子串匹配是这类代码最常见的写法，也是最容易放进来一个"看着像自己人"的域名。
    check("前缀相同不算匹配（`a.example.evil.com` ≠ `a.example`）",
          cors_headers("http://a.example", "http://a.example.evil.com") == {}, "")
    check("显式写 `*` 才回 `*`（这是运维自己选的，照它说的回）",
          cors_headers("*", "http://any.example").get("Access-Control-Allow-Origin")
          == "*", "")
    check("Gateway 的默认就是「不放行」（不许在构造函数里塞个默认来源）",
          Gateway(None, None).cors_origin is None, "")

    # ---- 线路上 ----
    class _Gw(object):
        """只当 Holder 用：Handler 读这几个字段。"""

        verbose = False
        started_at = 0.0

        class backend(object):                              # noqa: N801
            sessions = 2
            ctx_size = 8192

            @staticmethod
            def alive():
                return True

        class pool(object):                                 # noqa: N801
            @staticmethod
            def snapshot():
                return {"slots": [{"state": "free"}, {"state": "free"}], "waiting": []}

    saved_gw = Handler.gateway
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            resp.read()
            return {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    def preflight(headers):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("OPTIONS", "/v1/chat/completions", headers=headers)
            resp = conn.getresponse()
            resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    try:
        # 默认网关（cors_origin 缺省）下，三条出口一个 CORS 头都不许有
        Handler.gateway = _Gw()
        Handler.gateway.cors_origin = None
        for path in ("/health", "/demo"):
            h = get(path, {"Origin": "http://somebody-elses-page.example"})
            check("%s 带 Origin 也不发 ACAO（浏览器读不到响应）" % path,
                  "access-control-allow-origin" not in h, "= %r" % (h.get("access-control-allow-origin"),))
            check("%s 连 expose 头也不发" % path,
                  "access-control-expose-headers" not in h, "")
        status, h = preflight({"Origin": "http://somebody-elses-page.example",
                               "Access-Control-Request-Method": "POST"})
        check("预检仍是 204（拿 4xx/5xx 去「拒绝」只会变成报错，拦人的活儿浏览器自己会干）",
              status == 204, "status=%d" % status)
        check("预检里 ACAO 缺席 => 浏览器自己就把这个跨源请求拦了",
              "access-control-allow-origin" not in h, "")
        check("预检里也不再广告 Allow-Methods/Headers",
              "access-control-allow-methods" not in h
              and "access-control-allow-headers" not in h, "")

        # 配了来源之后：匹配的放行、不匹配的一样不发
        Handler.gateway.cors_origin = "http://a.example"
        h = get("/health", {"Origin": "http://a.example"})
        check("配了之后匹配来源能读到响应",
              h.get("access-control-allow-origin") == "http://a.example", "= %r" % (h.get("access-control-allow-origin"),))
        h = get("/health", {"Origin": "http://a.example.evil.com"})
        check("配了之后不匹配来源（子串那种）照样读不到",
              "access-control-allow-origin" not in h, "")
        status, h = preflight({"Origin": "http://a.example",
                               "Access-Control-Request-Method": "POST",
                               "Access-Control-Request-Headers": "X-Conversation-Id"})
        check("配了之后预检把自定义头也放行（否则网页端发 X-Conversation-Id 会被拦）",
              h.get("access-control-allow-origin") == "http://a.example"
              and "X-Conversation-Id" in (h.get("access-control-allow-headers") or ""),
              "= %r" % (h.get("access-control-allow-headers"),))
        # 同源的演示页**没有 Origin 头**，这是它一直能用的原因，得钉住。
        h = get("/demo", {})
        check("同源（请求里没有 Origin）的 /demo 不受影响",
              "access-control-allow-origin" not in h and int(h.get("content-length") or 0) > 0,
              "len=%r" % (h.get("content-length"),))
    finally:
        httpd.shutdown()
        httpd.server_close()
        Handler.gateway = saved_gw


def selftest_window(check):
    """改动 A：窗口裁剪的纯逻辑（不需要板卡/后端）。

    用一把与桩后端/真后端同量级的"字节/5"计数器代替 COUNT 帧，验四件事：
    超预算会丢、system 必留、最近一轮必留、没超预算/预算未知时原样不动。
    """
    class _Counter(object):
        ctx_size = 1000
        default_max_new_tokens = 64

        def count(self, text):
            return max(1, len(text.encode("utf-8")) // 5)

    msgs = [{"role": "system", "content": "S" * 1000}]
    for i in range(12):
        msgs.append({"role": "user", "content": "u%d " % i + "x" * 100})
        msgs.append({"role": "assistant", "content": "a%d " % i + "y" * 100})
    msgs.append({"role": "user", "content": "最新问题"})

    windowed, dropped = fit_window(_Counter(), msgs, 400, None, None, 2)
    check("超预算时确实丢了消息", dropped > 0, "dropped=%d, 余 %d 条"
          % (dropped, len(windowed)))
    check("system 一定被保留",
          any(m.get("role") == "system" for m in windowed), "")
    check("最近一轮 user 一定被保留",
          windowed[-1].get("content") == "最新问题", repr(windowed[-1]))
    check("插入了“历史已省略”占位",
          any("earlier history omitted" in str(m.get("content", ""))
              for m in windowed), "")
    check("裁剪后装进预算",
          _Counter().count(render_messages(windowed)) <= 400,
          "%d tok" % _Counter().count(render_messages(windowed)))

    small = [{"role": "user", "content": "hi"}]
    w2, d2 = fit_window(_Counter(), small, 400, None, None, 2)
    check("没超预算就原样不动", d2 == 0 and w2 == small, "dropped=%d" % d2)

    w3, d3 = fit_window(_Counter(), msgs, None, None, None, 2)
    check("预算未知（None）就不裁", w3 is None and d3 == 0, "dropped=%d" % d3)

    w4, d4 = fit_window(None, msgs, 400, None, None, 2)
    check("后端不可用就不裁", w4 is None and d4 == 0, "dropped=%d" % d4)


def selftest_window_units(check):
    """窗口裁剪必须保持 tool_use/tool_result 成对（不产生残缺对话）。"""
    class _Counter(object):
        def count(self, text):
            return max(1, len(text.encode("utf-8")) // 3)

    def assistant(tc=False):
        m = {"role": "assistant", "content": "a" * 200}
        if tc:
            m["tool_calls"] = [{"id": "call_0", "type": "function",
                                "function": {"name": "f", "arguments": "{}"}}]
        return m

    msgs = [{"role": "system", "content": "S" * 100},
            {"role": "user", "content": "u0" + "x" * 300},
            assistant(),
            {"role": "user", "content": "u1" + "y" * 300},
            assistant(tc=True),
            {"role": "tool", "content": "r0" + "z" * 300},
            assistant(),
            {"role": "user", "content": "最新问题"}]

    for budget in (400, 300, 200, 120):
        out, _dropped = fit_window(_Counter(), msgs, budget, None, None, 2)
        if out is None:
            continue
        bad = 0
        for i, m in enumerate(out):
            role = m.get("role") or "user"
            if role == "tool":
                prev = out[i - 1] if i > 0 else {}
                if not (prev.get("role") == "assistant" and prev.get("tool_calls")):
                    bad += 1
            if role == "assistant" and m.get("tool_calls"):
                nxt = out[i + 1] if i + 1 < len(out) else {}
                if (nxt.get("role") or "") != "tool":
                    bad += 1
        check("窗口不拆散 tool 回合（budget=%d）" % budget, bad == 0,
              "bad=%d, 保留 %d 条" % (bad, len(out)))


def selftest_prompt_trim(check):
    """改动 B：工具块稳定化 + 超长 tool 结果截断（纯逻辑，不需要板卡）。"""
    tA = {"type": "function", "function": {"name": "alpha"}}
    tB = {"type": "function", "function": {"name": "beta"}}
    check("工具按名字排序（与输入顺序无关）",
          [t["function"]["name"] for t in normalize_tools([tB, tA])]
          == ["alpha", "beta"], "")
    check("工具集为空也安全", normalize_tools([]) == [], "")

    short = "ok result"
    check("短结果原样不动", truncate_tool_result(short, 64) == short, "")

    big = ("头" * 1000) + ("X" * 20000) + ("尾" * 1000)
    cut = truncate_tool_result(big, 4096)
    check("超长结果被截断", len(cut.encode("utf-8")) < len(big.encode("utf-8")), "")
    check("截断标记存在", "truncated" in cut, "")
    check("头尾都保留", cut.startswith("头") and cut.endswith("尾"), "")
    check("截断确定性（同输入同输出）", truncate_tool_result(big, 4096) == cut, "")
    check("截断后没有半个 UTF-8 字（能无损回编码）",
          cut.encode("utf-8").decode("utf-8") == cut, "")

    global _TOOL_OUTPUT_MAX_BYTES
    old = _TOOL_OUTPUT_MAX_BYTES
    try:
        _TOOL_OUTPUT_MAX_BYTES = 4096
        trimmed = render_messages([{"role": "tool", "content": big}])
        _TOOL_OUTPUT_MAX_BYTES = 0
        full = render_messages([{"role": "tool", "content": big}])
    finally:
        _TOOL_OUTPUT_MAX_BYTES = old
    check("渲染后的工具结果确实变短",
          len(trimmed.encode("utf-8")) < len(full.encode("utf-8")),
          "%d vs %d bytes" % (len(trimmed.encode("utf-8")), len(full.encode("utf-8"))))


def selftest_swap(check):
    """改动 C：会话换页的纯逻辑（内存假后端，不连板卡）。"""
    class _FakeBackend(object):
        def __init__(self):
            self.sessions = 2
            self.saved = {}
            self.loaded = []
            self.on_clear = None
            self.protocol_v2 = True          # 换页需要后端认识 SAVE/LOAD 新帧

        def count(self, text):
            return max(1, len(text.encode("utf-8")) // 5)

        def alive(self):
            return True

        def save_kvcache(self, session, path):
            self.saved[path] = True
            return True

        def load_kvcache(self, session, path, ctx_tokens=0):
            self.loaded.append((session, path, ctx_tokens))
            return self.saved.get(path, False)

    d = tempfile.mkdtemp(prefix="kvswap-")
    be = _FakeBackend()
    pool = SessionPool(be, idle_ttl=300, queue_timeout=5, contend_idle=15,
                       kv_swap_dir=d)
    with pool.cv:
        pool.bound["alice"] = 0
        pool.last_used["alice"] = 0.0
        pool.known[0] = "A" * 100
        pool._reap_idle_locked(time.time(), ttl=1.0, limit=1)
    check("回收时把 KV 存了盘", "alice" in pool.swapped,
          "swapped=%d" % len(pool.swapped))
    check("换出后会话内容标未知", pool.known[0] is None, "")

    lease = pool.acquire("alice", "A" * 100 + "B" * 20)
    check("同 key 回来读回 KV（load 被调用一次）", len(be.loaded) == 1,
          "loaded=%r" % (be.loaded,))
    check("读回后按前缀只发增量",
          lease.base == "A" * 100 and lease.sent_prompt == "B" * 20,
          "base=%d sent=%d" % (len(lease.base), len(lease.sent_prompt)))
    pool.release(lease, "reply", False)
    pool.close("alice")
    check("close 清掉盘上记录", "alice" not in pool.swapped,
          "swapped=%d" % len(pool.swapped))
    check("换页计数可观测", pool.swap_out_total == 1 and pool.swap_in_total == 1,
          "out=%d in=%d" % (pool.swap_out_total, pool.swap_in_total))


def selftest_tool_compact(check):
    """超预算时压缩工具定义、保住可调用性（而不是整块丢弃）。"""
    big = [{"type": "function",
            "function": {"name": "t%d" % i, "description": "D" * 2000,
                         "parameters": {"type": "object", "title": "x",
                                        "properties": {"p": {"type": "string",
                                                             "description": "E" * 500}}}}}
           for i in range(10)]
    full = len(render_messages([], tools=big).encode("utf-8"))
    c1 = compact_tools(big, 1)
    c2 = compact_tools(big, 2)
    b1 = len(render_messages([], tools=c1).encode("utf-8"))
    b2 = len(render_messages([], tools=c2).encode("utf-8"))
    check("压缩后确实更小", b1 < full and b2 < b1,
          "full=%d c1=%d c2=%d" % (full, b1, b2))
    check("压缩后工具数量不变", len(c1) == len(big) and len(c2) == len(big), "")
    check("压缩后仍保留工具名", c2[0]["function"]["name"] == "t0", "")
    check("压缩后仍保留参数结构", "properties" in c2[0]["function"]["parameters"], "")

    class _Counter(object):
        def count(self, text):
            return max(1, len(text.encode("utf-8")) // 3)

    msgs = [{"role": "system", "content": "S" * 100},
            {"role": "user", "content": "hi"}]
    _fm, ft, _d, mode, _n = budget_fit(_Counter(), msgs, big, None, 2000, 2)
    check("budget_fit 压缩而非丢弃工具",
          mode in ("compact1", "compact2") and len(ft) == len(big),
          "mode=%s tools=%d" % (mode, len(ft)))


def selftest_perf(check):
    """性能统计的纯算术（不碰后端）：计数、分位数、总吞吐、KV 命中率、气泡比。

    这些数就是算法 A/B 的判据本身，算错了整场对比都白做——所以拿确定性输入钉死。
    """
    pc = PerfCollector()
    base = time.monotonic()
    # 3 条记录：两条成功（不同速率）+ 一条被拒（没有阶段数据）
    cases = [
        (100, 200.0, 20, 400.0, 300.0),
        (400, 400.0, 40, 800.0, 900.0),
        (0, 0.0, 0, 0.0, None),
    ]
    for i, (pf, pf_ms, dc, dc_ms, ttft) in enumerate(cases):
        pc.add({
            "rid": i + 1, "session": i, "ok": i < 2,
            "finish": "stop" if i < 2 else None,
            "error": None if i < 2 else "context limit reached",
            "rejected": i == 2,
            "queue_wait_s": 0.5 * i if i < 2 else None,
            "ttft_ms": ttft,
            "e2e_ms": (((ttft or 0.0) + dc_ms) if i < 2 else None),
            "prefill_tokens": pf, "prefill_ms": pf_ms,
            "prefill_tok_s": (pf / (pf_ms / 1000.0)) if pf_ms else None,
            "decode_tokens": dc, "decode_ms": dc_ms,
            "decode_tok_s": (dc / (dc_ms / 1000.0)) if dc_ms else None,
            "tpot_ms": (dc_ms / dc) if dc else None,
            "prompt_tokens": pf + dc, "cached_tokens": pf,
            "ctx_tokens": pf + dc,
            "reset": True, "sent_bytes": 0, "base_bytes": 0, "full_bytes": 0,
            "t_submit": base + i, "t_done": base + i + 1,
        })
    doc = pc.snapshot(2, full=False)
    check("窗口计数（3 条，2 条成功）",
          doc["window"]["n"] == 3 and doc["window"]["ok"] == 2,
          "n=%d ok=%d" % (doc["window"]["n"], doc["window"]["ok"]))
    check("被拒单独计数、不算 error", doc["window"]["rejected"] == 1
          and doc["window"]["errors"] == 0,
          "rejected=%d errors=%d" % (doc["window"]["rejected"], doc["window"]["errors"]))
    check("TTFT p50/max（300/900）",
          doc["ttft_ms"]["p50"] == 300.0 and doc["ttft_ms"]["max"] == 900.0,
          str(doc["ttft_ms"]))
    check("prefill 总吞吐 = 500 tok / 0.6 s",
          abs(doc["prefill"]["tok_s"] - 833.3) < 0.2, str(doc["prefill"]["tok_s"]))
    check("decode 总吞吐 = 60 tok / 1.2 s",
          abs(doc["decode"]["tok_s"] - 50.0) < 0.01, str(doc["decode"]["tok_s"]))
    check("TPOT p50 = 20 ms", doc["decode"]["tpot_ms_p50"] == 20.0,
          str(doc["decode"]["tpot_ms_p50"]))
    check("KV 命中率 = 500/560", abs(doc["kv"]["cache_hit_ratio"] - 500.0 / 560.0) < 1e-4,
          str(doc["kv"]["cache_hit_ratio"]))
    check("气泡比在 [0,1] 且有值", doc["slots"]["bubble_ratio"] is not None
          and 0.0 <= doc["slots"]["bubble_ratio"] <= 1.0, str(doc["slots"]))
    check("full=1 带明细", len(pc.snapshot(2, full=True)["requests"]) == 3)
    check("reset 清零", pc.reset() == 3 and pc.snapshot(2)["window"]["n"] == 0)
    check("空窗口不编数（全 None / 0）",
          pc.snapshot(2)["ttft_ms"]["p50"] is None
          and pc.snapshot(2)["window"]["n"] == 0)


def selftest(backend, pool):
    """验证 C++ 侧的帧协议。比"直接上 HTTP"分段定位得清楚：协议不通时不用怀疑 HTTP。"""
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))

    print("== READY ==")
    check("sessions", backend.sessions == pool.n, "sessions=%d" % backend.sessions)
    check("default max_new_tokens", backend.default_max_new_tokens > 0,
          "= %d" % backend.default_max_new_tokens)

    print("== max_tokens 夹到后端能接受的范围（否则一个离谱的值能打死一个会话）==")
    check("不传 => 0（用后端默认值）", _pick_max_tokens({}) == 0,
          "= %d" % _pick_max_tokens({}))
    check("正常值原样透传", _pick_max_tokens({"max_tokens": 2000}) == 2000,
          "= %d" % _pick_max_tokens({"max_tokens": 2000}))
    check("超大值被夹到上限（不是原样发出去）",
          _pick_max_tokens({"max_tokens": 10 ** 12}) == MAX_NEW_TOKENS_CAP,
          "10**12 -> %d" % _pick_max_tokens({"max_tokens": 10 ** 12}))
    check("上限本身不变", _pick_max_tokens({"max_tokens": MAX_NEW_TOKENS_CAP}) == MAX_NEW_TOKENS_CAP,
          "= %d" % MAX_NEW_TOKENS_CAP)

    print("== 性能统计（纯算术；算法 A/B 的判据本身）==")
    selftest_perf(check)

    print("== MLFQ 调度内核（纯算术；端侧五因子近似）==")
    mlfq_sched.selftest(check)

    print("== 单请求（非流式）==")
    p1 = render_messages([{"role": "user", "content": "用一句话说明你是谁。"}])
    t0 = time.time()
    q = backend.submit(p1, 32, session=-1, reset=False)
    backend.wait(q)
    dt = time.time() - t0
    check("有回复", len(q.text()) > 0, "%.1fs, %d 字, session=%d, finish=%s"
          % (dt, len(q.text()), q.session, q.finish_reason))
    check("有 decode 计数", q.decode_tokens > 0,
          "prefill=%d decode=%d ctx=%d" % (q.prefill_tokens, q.decode_tokens, q.context_tokens))
    s1 = q.session
    backend.release_request(q)

    print("== 流式（on_delta 回调）==")
    chunks = []
    q = backend.submit(render_messages([{"role": "user", "content": "数到三。"}]), 24,
                       session=s1, reset=True)
    q.on_delta = lambda t: chunks.append(t)
    backend.wait(q)
    joined = "".join(chunks)
    check("收到多个 delta", len(chunks) > 1, "%d 个 delta" % len(chunks))
    check("delta 拼起来 == 整段", joined == q.text(),
          "delta=%d 字, full=%d 字" % (len(joined), len(q.text())))
    backend.release_request(q)

    print("== 会话粘性：只发差异部分应该更快 ==")
    # 先在一个会话里铺一段上下文，然后分别用「全量 + RESET」和「只发差异」跑同一轮，
    # 比 prefill token 数——这是复用生效最直接的证据。
    base_msgs = [{"role": "user", "content": "请记住这句话：" + "麒麟九千" * 60}]
    q = backend.submit(render_messages(base_msgs), 8, session=s1, reset=True)
    backend.wait(q)
    first_ans = q.text()
    backend.release_request(q)
    known = render_messages(base_msgs) + first_ans

    follow = base_msgs + [{"role": "assistant", "content": first_ans},
                          {"role": "user", "content": "继续。"}]
    full = render_messages(follow)
    suffix = full[len(known):]
    check("差异部分确实短得多", 0 < len(suffix) < len(full),
          "full=%d 字, suffix=%d 字" % (len(full), len(suffix)))

    q2 = backend.submit(suffix, 8, session=s1, reset=False)
    backend.wait(q2)
    backend.release_request(q2)
    q3 = backend.submit(full, 8, session=s1, reset=True)
    backend.wait(q3)
    backend.release_request(q3)
    check("复用比全量重算 prefill 少", q2.prefill_tokens < q3.prefill_tokens,
          "复用 prefill=%d tok, 全量 prefill=%d tok"
          % (q2.prefill_tokens, q3.prefill_tokens))

    print("== 关思考时摘掉开头那个（空的）think 段 ==")
    # 这是纯字符串逻辑，不该等板卡——本地一次跑完。逐字符投递那一组是关键：DELTA 是按
    # token 投的，标签完全可能被切成两帧（"<thi" + "nk>"），逐帧正则一定会漏。
    think_cases = [
        ("<think>  </think>  收到", "收到"),
        ("<think>\n</think>\n7 8 9", "7 8 9"),
        ("收到", "收到"),
        ("<think>推理没闭合", "<think>推理没闭合"),   # 被截断：原样交还，不吞正文
        ("<thi", "<thi"),
    ]
    for raw, want in think_cases:
        got1 = strip_think(raw, False)
        s = ThinkStripper()
        got2 = "".join(s.feed(c) for c in raw) + s.flush()
        check("摘 think: %r -> %r" % (raw, want), got1 == want and got2 == want,
              "一次投=%r, 逐字投=%r" % (got1, got2))
    check("开思考时原样透传（不擅自丢推理过程）",
          strip_think("<think>推理</think>答案", True) == "<think>推理</think>答案"
          and strip_think("<think>推理</think>答案", None) == "<think>推理</think>答案")

    # 工具调用的解析也是纯字符串逻辑，同样不必等板卡。放在这里而不是这里重写一遍：
    # 实现和测试同住 toolcalls.py，check_template.py 也调同一个（两处都不需要后端）。
    toolcalls_selftest(check)

    print("== 会话池：RESET+CLEAR 之后仍要能复用（回归）==")
    # 这条是补的：原实现里 release() 在 was_cleared 时把 known 清成空串，于是每段对话
    # 只要第一轮需要 RESET（会话里装着上一段对话），后面每一轮都退化成全量重算，还会把
    # 历史重复堆进 KV。它在"每个对话都拿到干净会话"的测试里完全看不出来，只在会话变脏
    # 之后现形——所以必须专门造出"所有会话都脏"的局面来测。
    def pool_turn(key, prompt, n=8):
        lease = pool.acquire(key, prompt)
        qq = backend.submit(lease.sent_prompt, n, session=lease.session, reset=lease.reset)
        backend.wait(qq)
        pool.release(lease, qq.text(), qq.was_cleared)
        backend.release_request(qq)
        return lease, qq

    def arrange(known, bindings=None):
        """把会话池摆到一个**确定**的初态，并返回原状态。

        为什么要这么写：acquire 会优先挑"没主、没装过东西"的会话，所以池子的历史状态
        会改变它走哪条分支——靠前面的 section 顺手留下的状态来构造场景，断言随时会因为
        上游改动而失去意义（本次就撞上了：加了"优先挑没主的会话"之后，原断言里"所有
        会话都脏"这个前提不成立了）。显式摆状态，测试才是在测它声称要测的东西。
        """
        saved = (list(pool.known), dict(pool.bound), dict(pool.last_used),
                 list(pool.busy), pool.idle_ttl)
        pool.known = list(known)
        pool.bound = dict(bindings or {})
        # last_used 必须一起清：空闲回收（reap）是按它判定的，留着上一段的时刻会让
        # "某段对话静默了多久"变成一个随执行顺序变化的量，断言就失去意义了。
        # 清空之后 reap 里的 `last_used.get(k, now)` 会给所有 key 默认"刚刚才用过"，
        # 于是只有测试显式往前拨过的那些 key 会被回收——这正是我们要的确定性。
        pool.last_used = {}
        return saved

    def restore(saved):
        (pool.known, pool.bound, pool.last_used,
         pool.busy, pool.idle_ttl) = (list(saved[0]), dict(saved[1]), dict(saved[2]),
                                      list(saved[3]), saved[4])

    # 初态：每个会话都"清过 KV、内容未知"（实测里这是常态——每次 RESET 都伴随 CLEAR），
    # 且没有任何绑定。
    saved = arrange([None] * pool.n)
    m1 = [{"role": "user", "content": "请记住这句话：" + "麒麟九千" * 60}]
    lease_a, q_a = pool_turn("reuse-after-reset", render_messages(m1))
    check("会话都脏了 => 这一轮走的是 RESET", lease_a.reset)
    check("后端回了 CLEAR（本轮 KV 被清过）", q_a.was_cleared,
          "ctx=%d" % q_a.context_tokens)
    m2 = m1 + [{"role": "assistant", "content": q_a.text()},
               {"role": "user", "content": "继续。"}]
    p2 = render_messages(m2)
    lease_b, q_b = pool_turn("reuse-after-reset", p2)
    check("RESET 之后第二轮仍然只发差异部分", bool(lease_b.base) and not lease_b.reset
          and len(lease_b.sent_prompt) < len(p2),
          "sent=%d / full=%d 字, base=%d 字, reset=%s"
          % (len(lease_b.sent_prompt), len(p2), len(lease_b.base), lease_b.reset))
    check("复用那轮 prefill 确实更少", q_b.prefill_tokens < q_a.prefill_tokens,
          "%d vs %d tok" % (q_b.prefill_tokens, q_a.prefill_tokens))

    print("== 会话池：新对话不许被派到别人占着的会话上（否则两段对话只能互相等）==")
    # 这条是实测撞出来的：http_scaling 在同一构建上量到过 3.40x，也量到过 1.90x / 1.00x。
    # 根因是 acquire 挑空闲会话时只看 busy、不看 bound——"空闲"的会话可能正被另一段
    # 对话绑着（只是此刻没在跑），新对话被派过去之后两段对话共用一个会话，后端同一会话
    # 一次只跑一条，于是它们**只能轮流跑**：答案全对、并发掉一半，而且退化成不成全看
    # 请求到达顺序（谁先抢到谁的绑定就赢），所以是偶发。
    # 构造要点：让被绑着的那些会话 known 都是 None（清过 KV 的常态——实测现场就是这样），
    # 否则 min() 的备选项排序会"顺手"避开它，掩盖这个 bug。
    restore(saved)
    arrange([None] * pool.n, {"owner-x": 0})
    lease_y, _ = pool_turn("owner-y", render_messages([{"role": "user", "content": "乙"}]))
    check("新对话没有落到 owner-x 占着的会话上（s0）",
          lease_y.session != 0 or pool.n == 1,
          "新对话拿到 s%d（共 %d 个会话）" % (lease_y.session, pool.n))
    # 上限路径：所有会话都有主时，新对话**排队等**，不许把谁的会话夺过来。
    # 这条以前断言的是相反的（"仍能派活且旧主人被解绑"）——那时的取舍是"响应快"，
    # 代价是被抢的那段对话下一轮前缀对不上、要全量重算，而且谁被抢**取决于请求到达
    # 顺序**。现在改成可预期的排队：等 + 空闲回收（见下一条）。
    arrange([None] * pool.n, dict(("fill-%d" % i, i) for i in range(pool.n)))
    before = dict(pool.bound)
    try:
        pool.acquire("intruder", render_messages([{"role": "user", "content": "来抢"}]),
                     timeout=0.3)
        check("会话全都有主时排队等待（不抢占）", False, "竟然拿到了会话")
    except BackendError as exc:
        check("会话全都有主时排队等待（不抢占）",
              dict(pool.bound) == before and "no session available" in str(exc),
              "超时报错且绑定未变（%d 个会话全都有主）" % pool.n)
    check("排队失败后不留残留", "intruder" not in pool.waiting and "intruder" not in pool.bound)

    # 空闲回收：一段对话静默超过 TTL，它的会话要交出来——**这是"排队"能结束的唯一机制**，
    # 没有它，第 N+1 段对话永远等不到（网关不知道谁"说完了"，客户端不发结束消息、浏览器
    # 关了也没人通知）。构造：所有会话都有主 + 显式把 fill-0 的"最后提问时刻"往前拨。
    saved_ttl, saved_reaped = pool.idle_ttl, pool.reaped
    pool.idle_ttl = 0.2
    pool.last_used["fill-0"] = time.time() - 10.0
    # 这里直接 acquire 而不是 pool_turn：要断言的 reset/known 是 **acquire 那一刻**的状态，
    # pool_turn 会顺手 release 掉，把 known 又写成确定值，断言就落空了。
    lease_w = pool.acquire("waiter", render_messages([{"role": "user", "content": "轮到我了吗"}]))
    check("静默超时的对话被回收，排队的立刻拿到会话",
          pool.reaped == saved_reaped + 1 and "fill-0" not in pool.bound
          and lease_w.waited < 1.0,
          "reaped %d->%d，waiter 等了 %.2fs 拿到 s%d"
          % (saved_reaped, pool.reaped, lease_w.waited, lease_w.session))
    check("回收后该会话内容未知 => 强制 RESET（否则新对话的全量 prompt 会追加在旧上下文后）",
          lease_w.reset is True and pool.known[lease_w.session] is None,
          "known=%r reset=%s" % (pool.known[lease_w.session], lease_w.reset))
    pool.release(lease_w, "", True)
    # 回收只影响**闲着的**那段对话。正在跑的那一轮不能被收走：收走也没用（它马上
    # 就还回来），只会让它在跑到一半时被判成"空闲"、正跑着的 KV 被标成"内容未知"。
    s1_busy = pool.bound["fill-1"]
    pool.last_used["fill-1"] = time.time() - 10.0
    pool.busy[s1_busy] = True
    n_reaped = pool.reaped
    with pool.cv:
        pool._reap_idle_locked(time.time())
    pool.busy[s1_busy] = False
    check("正在跑的会话不被回收",
          pool.reaped == n_reaped and "fill-1" in pool.bound,
          "fill-1 在 s%d 上跑着，仍在 bound 里" % s1_busy)
    pool.idle_ttl = saved_ttl

    print("== 会话池：有人在等时按 contend_idle 收（NPU 空着不许让人干等）==")
    # 用户 2026-09-18 报的："明明 NPU 没人使用，但是我输入问题后还是在等待"。
    # 机制：**只有 idle_ttl 一条路决定谁腾会话**，而它必须调得很大（300s）才保得住多轮
    # 对话的 KV 复用。于是四段对话把四个会话绑住后，第 5 段对话要等到某一段静默满 300s；
    # 那段时间里四个会话全是 idle、NPU 一个 token 都没在算。板上实测等了 226.0s。
    # 修法：**已经有请求非等不可**（进来发现一个空闲会话都没有）时，改用 contend_idle
    # 这一级：只收**最闲的**那一段，且只收一个。没人排队时这一级完全不生效——常规路径
    # 逐字节不变，这也是下面第一条断言要钉的。
    saved_ttl3, saved_contend, saved_reaped3 = (pool.idle_ttl, pool.contend_idle,
                                                pool.reaped)
    pool.idle_ttl, pool.contend_idle = 999.0, 1.0
    # ① 常规路径：还有空闲会话可用时，**不许**用第二级阈值把别人踢掉。
    #    这里 fill-0 已经静默 30s（远超 contend_idle=1.0），但只要还有空会话，它就该
    #    原地留着——它的 KV 是下一轮复用的本钱，为了"顺手腾地方"扔掉就是白花 prefill。
    if pool.n >= 2:
        arrange([None] * pool.n, dict(("fill-%d" % i, i) for i in range(pool.n - 1)))
        pool.last_used["fill-0"] = time.time() - 30.0
        lease_free = pool.acquire("newbie", render_messages([{"role": "user", "content": "甲"}]))
        check("还有空闲会话时，第二级回收不生效（常规路径不变）",
              pool.reaped == saved_reaped3 and "fill-0" in pool.bound,
              "reaped %d->%d，fill-0 %s"
              % (saved_reaped3, pool.reaped,
                 "仍在 bound 里" if "fill-0" in pool.bound else "被踢了"))
        pool.release(lease_free, "", True)

        # ② 抢手时：一个空闲会话都没有 => 立刻收**最闲的那一段**，而且只收一个。
        #    fill-0 静默 5s、fill-1 静默 60s：受害者必须是 fill-1。按静默挑而不是按
        #    dict 顺序，行为才可预期（当初决定"不抢占"就是怕"谁被抢看到达顺序"）。
        arrange([None] * pool.n, dict(("fill-%d" % i, i) for i in range(pool.n)))
        pool.last_used["fill-0"] = time.time() - 5.0
        pool.last_used["fill-1"] = time.time() - 60.0
        # fill-2（如果有）正在跑：再闲也不许收——收走没用（它马上就还回来），还会让
        # 正跑着的那一轮的 KV 被标成"内容未知"。
        s_busy = pool.bound.get("fill-2")
        if s_busy is not None:
            pool.busy[s_busy] = True
            pool.last_used["fill-2"] = time.time() - 120.0
        t0q = time.time()
        lease_c = pool.acquire("late-comer", render_messages([{"role": "user", "content": "乙"}]))
        took = time.time() - t0q
        check("一个空闲会话都没有时，立刻收最闲的那一段（不再等到 idle_ttl）",
              lease_c.waited < 1.0 and "fill-1" not in pool.bound
              and pool.reaped == saved_reaped3 + 1,
              "等了 %.2fs，reaped %d->%d，拿到 s%d"
              % (lease_c.waited, saved_reaped3, pool.reaped, lease_c.session))
        check("收的是**最闲的**那一段（fill-1 静默 60s > fill-0 的 5s）",
              "fill-0" in pool.bound, "bound=%s" % sorted(pool.bound))
        check("只收一个（多收等于把几段对话的 KV 一起扔掉）", pool.reaped == saved_reaped3 + 1)
        if s_busy is not None:
            check("正在跑的会话即使在抢手时也不被收", "fill-2" in pool.bound,
                  "fill-2 在 s%d 上跑着" % s_busy)
            pool.busy[s_busy] = False
        check("收走的那段对话回来必须 RESET（内容已归别人）",
              lease_c.reset is True and pool.known[lease_c.session] is None,
              "known=%r reset=%s" % (pool.known[lease_c.session], lease_c.reset))
        pool.release(lease_c, "", True)
    else:
        print("  [SKIP] 只有 %d 个会话，跳过" % pool.n)

    # ③ contend_idle=0 = 关掉这一级，退回老行为（排到 idle_ttl 为止）。留这个开关是
    #    为了让"到底该不该抢"这件事可以被现场实验推翻，而不是写死在代码里。
    if pool.n >= 2:
        pool.contend_idle = 0.0
        arrange([None] * pool.n, dict(("fill-%d" % i, i) for i in range(pool.n)))
        pool.last_used["fill-1"] = time.time() - 600.0
        try:
            pool.acquire("waiter-2", render_messages([{"role": "user", "content": "丙"}]),
                         timeout=0.3)
            check("contend_idle=0 时退回老行为（只排队，不回收）", False, "竟然拿到了会话")
        except BackendError:
            check("contend_idle=0 时退回老行为（只排队，不回收）",
                  "fill-1" in pool.bound and pool.reaped == saved_reaped3 + 1,
                  "超时报错，绑定未变")
    pool.idle_ttl, pool.contend_idle, pool.reaped = (saved_ttl3, saved_contend,
                                                     saved_reaped3)

    # 无身份的请求（key=None）**不许记绑定**：记了的话 bound[None] 只有一条，所有匿名
    # 请求都会读到它，于是素不相识的几段对话被钉到同一个会话上互相等——正好是这个池子
    # 存在的意义的反面。这条是差点被我改丢的：原来的代码有 `if key is not None`，
    # 重写排队逻辑时漏了，而漏了它**不会报任何错**，只是并发静默退化。
    arrange([None] * pool.n)
    lease_n1 = pool.acquire(None, render_messages([{"role": "user", "content": "匿名甲"}]))
    pool.release(lease_n1, "", True)
    lease_n2 = pool.acquire(None, render_messages([{"role": "user", "content": "匿名乙"}]))
    pool.release(lease_n2, "", True)
    check("无身份的请求不记绑定（否则所有匿名请求会共用一个会话）",
          None not in pool.bound and lease_n1.session != lease_n2.session,
          "bound 里没有 None；两次匿名请求落在 s%d / s%d"
          % (lease_n1.session, lease_n2.session))

    # 主动交还（`POST /v1/conversations/close`）：客户端说"我走了"，会话立刻回到池子里，
    # 不用等 IDLE_TTL。这是"排队"在真实使用里能成立的前提之一——否则一个连着问 4 个
    # 不相干问题的脚本会把 4 个会话全占住，后面的人等满一个 TTL。
    arrange([None] * pool.n)
    lease_c = pool.acquire("alice", render_messages([{"role": "user", "content": "甲"}]))
    pool.release(lease_c, "答完了", True)
    s_alice = lease_c.session
    check("交还前：这段对话占着会话", pool.bound.get("alice") == s_alice,
          "alice 在 s%d" % s_alice)
    got = pool.close("alice")
    check("close 放掉了它占的会话", got == s_alice and "alice" not in pool.bound
          and pool.last_used.get("alice") is None,
          "关闭返回 s%s，bound=%s" % (got, dict(pool.bound)))
    check("交还后内容标成未知（下一个拿到的人必须 RESET，不能把新 prompt 追加在旧 KV 后）",
          pool.known[s_alice] is None, "known[s%d]=%r" % (s_alice, pool.known[s_alice]))
    check("重复 close 是幂等的（返回 None，不报错）", pool.close("alice") is None)
    # 交还之后，**新的**对话应该能立刻拿到这个会话，而不是排队等 TTL
    saved_ttl2 = pool.idle_ttl
    pool.idle_ttl = 999.0          # 关掉回收：这里要证明的是 close 起了作用，不是 TTL 起了作用
    pool.busy = [False] * pool.n   # 其余会话都装成"忙"，只留 s_alice 一个是空的
    for s in range(pool.n):
        if s != s_alice:
            pool.busy[s] = True
    t0c = time.time()
    lease_d = pool.acquire("bob", render_messages([{"role": "user", "content": "乙"}]))
    check("交还出来的会话能被新对话立刻拿到（不用等 TTL）",
          lease_d.session == s_alice and lease_d.waited < 0.5,
          "bob 等了 %.2fs 拿到 s%d" % (lease_d.waited, lease_d.session))
    # 正在跑的那一轮**不许**被抽走会话：那会让它脚下的 KV 被下一个人 RESET 掉。
    pool.busy[lease_d.session] = True
    try:
        pool.close("bob")
        busy_refused = False
    except BackendError:
        busy_refused = True
    pool.busy[lease_d.session] = False
    check("正在生成时 close 被拒（不能抽走跑着的会话）",
          busy_refused and pool.bound.get("bob") == lease_d.session)
    pool.release(lease_d, "", True)
    pool.idle_ttl = saved_ttl2
    restore(saved)

    print("== RESET 语义 ==")
    q = backend.submit(render_messages([{"role": "user", "content": "你好"}]), 8,
                       session=s1, reset=True)
    backend.wait(q)
    after = q.context_tokens
    backend.release_request(q)
    check("RESET 后上下文从头累计", after < 200, "本轮结束 ctx=%d" % after)

    print("== 并发：N 个会话同时跑 ==")
    if backend.sessions >= 2:
        n = min(backend.sessions, 4)
        res = [None] * n
        t0 = time.time()

        def worker(i):
            r = backend.submit(render_messages([{"role": "user", "content": "数到二十。"}]),
                               64, session=i, reset=True)
            backend.wait(r)
            res[i] = (r.decode_tokens, r.decode_ms)
            backend.release_request(r)

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.time() - t0
        tot = sum(x[0] for x in res if x)
        check("N 路都出了 token", all(x and x[0] > 0 for x in res),
              "合计 %d tok / %.1fs = %.2f tok/s（N=%d）" % (tot, wall, tot / wall, n))
    else:
        print("  [SKIP] 只有 %d 个会话，跳过并发项" % backend.sessions)

    print("== REJECT：后端拒了一轮，会话**不能**被标死 ==")
    if any("fake_backend" in str(a) for a in backend.argv):
        selftest_reject(backend, check)
    else:
        # 真后端没有 --ctx-limit，造不出这个帧序；真板上要把一段对话撑到 4096 token 才
        # 触发，那不叫自检、那叫压测。真后端那半个只能在板卡上手动验（把上下文跑满）。
        print("  [SKIP] 这一段要桩后端（--ctx-limit 是桩专有的开关）")

    print("== 后端死了 / 会话全 dead：不能让人排在队列里空等 ==")
    selftest_pool_liveness(check)

    print("== 资源观测：/proc 解析、CPU 百分比算术、STAT 帧 ==")
    selftest_system(check, backend)

    print("== 继续回答：acquire_continue + 记账（半截留账、续写拼回） ==")
    selftest_continue(check)

    selftest_cors(check)

    print("== 窗口裁剪（改动 A）：超预算丢最老、保 system + 近期 ==")
    selftest_window(check)

    print("== 窗口按完整回合丢：tool_use/tool_result 不成对不允许 ==")
    selftest_window_units(check)

    print("== 削冗余 prompt（改动 B）：工具稳定化 + 超长结果截断 ==")
    selftest_prompt_trim(check)

    print("== 工具压缩（超预算保工具可调用）：砍描述、留 name/parameters ==")
    selftest_tool_compact(check)

    print("== 会话换页（改动 C）：回收存盘、回来读回 ==")
    selftest_swap(check)

    print("\n%s" % ("自检全部通过" if ok else "自检有失败项，见上面的 FAIL"))
    return 0 if ok else 1


# ===========================================================================
# 6. main
# ===========================================================================

def main():
    global _VERBOSE, _TOOL_OUTPUT_MAX_BYTES, _TOOL_STABLE
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--host", default="127.0.0.1")
    # 端口 2026-09-20 从 8080 挪到 18280：8080 是"随手就能试"的那一个（几乎每个扫描器
    # 都会先碰它），而这个服务**没有鉴权**，被扫到就等于被用上。换端口本身**不是**安全
    # 控制（同一个局域网里扫一遍就找到了，见 start_gateway.sh 里对 HOST 的说明）——
    # 它的作用是让这台机器不再落在"默认端口"那份名单上，配合收紧 CORS 一起看。
    ap.add_argument("--port", type=int, default=18280)
    ap.add_argument("--cors-origin", default=None,
                    help="允许哪个**来源**跨源调用本服务（浏览器的那道门禁）。默认不给，"
                         "即一个跨源请求都不放行；演示页是同源的，用不到它。要放开就写全 "
                         "scheme://host[:port]（也可以写 `*`，但那等于把门拆了，别用）")
    ap.add_argument("--sessions", type=int, default=4,
                    help="期望的会话数。与后端 READY 报的对不上时**只警告、不退出**——"
                         "池子大小取自后端报的数（它才是真正知道几个会话可用的一方），"
                         "这个参数只是让你早点看见「参数和实际对不上」，真不一致也照样能服务")
    ap.add_argument("--backend-log", default="gateway_backend.log")
    ap.add_argument("--idle-ttl", type=float, default=DEFAULT_IDLE_TTL,
                    help="一段对话静默超过这么多秒就把它占的会话收回给排队者"
                         "（0 = 不回收，等于第 N+1 段对话永远排队）。默认 %.0f"
                         % DEFAULT_IDLE_TTL)
    ap.add_argument("--contend-idle", type=float, default=DEFAULT_CONTEND_IDLE,
                    help="**已经有请求非等不可**（一个空闲会话都没有）时用的静默阈值："
                         "静默超过这么多秒的对话所占的会话立刻收回给等的人，只收最闲的"
                         "那一个。没人排队时仍按 --idle-ttl。默认 %.0f；0 = 关掉这一级"
                         "（退回「一定要等到 --idle-ttl」）"
                         % DEFAULT_CONTEND_IDLE)
    ap.add_argument("--sched-policy", default=os.environ.get("SCHED_POLICY", "fcfs"),
                    choices=list(mlfq_sched.POLICIES),
                    help="会话分配策略。fcfs（默认）= 先到先得，行为与改造前逐字节一致；"
                         "mlfq = 五因子加权；sjf/las/stride/edf/prio/prefix = 见 gateway/mlfq_sched.py")
    ap.add_argument("--sched-weights",
                    default=os.environ.get("SCHED_WEIGHTS", "0.4,0.3,0.15,0.1,0.05"),
                    help="mlfq 五因子权重 cp,cr,sa,pf,tl（默认沿用服务端 mlfq 那组）")
    ap.add_argument("--sched-long-tokens", type=float,
                    default=float(os.environ.get("SCHED_LONG_TOKENS", 0)) or None,
                    help="mlfq 长任务阈值（估算 prompt token 数，超过就限流）；"
                         "默认 %d" % mlfq_sched.DEFAULT_LONG_TOKENS)
    ap.add_argument("--sched-long-max", type=int,
                    default=int(os.environ.get("SCHED_LONG_MAX", 0)) or None,
                    help="mlfq 长任务并发上限；默认 max(1, NSESSION//2)")
    ap.add_argument("--sched-cost", default=os.environ.get("SCHED_COST", "full"),
                    choices=["full", "delta"],
                    help="调度成本口径：full=整段 prompt token（默认，行为不变）；"
                         "delta=整段×(1−前缀复用比例)，更贴近真实 prefill 增量")
    ap.add_argument("--queue-timeout", type=float, default=DEFAULT_QUEUE_TIMEOUT,                    help="取不到会话时最多排队等这么多秒，超了返回 503。"
                         "默认 %.0f；应大于 --idle-ttl，否则会出现"
                         "「马上就要轮到了，请求却先超时」"
                         % DEFAULT_QUEUE_TIMEOUT)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--temp-file", default=_CARD_TEMP_FILE,
                    help="RK1828 结温的 JSON（由 rknn_smi_temp.py 以 root 写出，见 "
                         "rknn-temp.service）。默认 %(default)s；读不到就画「—」，"
                         "不影响任何别的功能")
    ap.add_argument("--selftest", action="store_true",
                    help="只验帧协议（不起 HTTP），用于把 C++ 侧和 HTTP 侧分开定位")
    ap.add_argument("--frames-stdout", action="store_true",
                    help="仅本地自检：帧从后端的 stdout 读（配桩后端 fake_backend.py）。"
                         "pass_fds 在 Windows 上不可用，板卡上用不到这个开关")
    # ---- 改动 A：窗口裁剪 ----
    ap.add_argument("--no-window", action="store_true",
                    help="关闭窗口裁剪。默认**开启**：prompt 超过上下文预算时，保留 system "
                         "+ 最近若干轮、丢掉最老的非 system 历史，让单会话能一直聊下去，"
                         "而不是攒长了就被后端 REJECT（HTTP 400）")
    ap.add_argument("--ctx-reserve", type=int, default=DEFAULT_CTX_RESERVE,
                    help="窗口预算给 prefill/解码尾巴留的余量（token），默认 %(default)s")
    ap.add_argument("--window-min-messages", type=int, default=DEFAULT_WINDOW_MIN_MESSAGES,
                    help="窗口至少保留的非 system 消息数（再挤也留住最近一问一答），"
                         "默认 %(default)s")
    # ---- 改动 B：削冗余 prompt ----
    ap.add_argument("--tool-output-max-bytes", type=int, default=_TOOL_OUTPUT_MAX_BYTES,
                    help="单条 tool 结果的字节上限，超出按 head+tail 截断（0=不截断）。"
                         "工具结果是上下文膨胀的大头，默认 %(default)s 字节")
    ap.add_argument("--no-tool-stable", action="store_true",
                    help="不按 function.name 排序工具定义。默认排序，消除工具顺序漂移导致"
                         "的 KV 前缀失效（静默变慢）")
    # ---- 改动 C：会话换页 ----
    ap.add_argument("--kv-swap-dir", default=None,
                    help="会话换页目录。给定后，被回收/让出的对话会把 KV 存到这里，同一段"
                         "对话再回来且拿到空闲会话时读回，免去一次全量 prefill。"
                         "默认关闭（不落盘）。建议指向 tmpfs，如 /dev/shm/rknn-kv")
    ap.add_argument("--kv-swap-cap", type=int, default=DEFAULT_KV_SWAP_CAP,
                    help="换页最多保留几段对话（按条数，LRU 淘汰），默认 %(default)s")
    ap.add_argument("--kv-swap-max-mb", type=int, default=DEFAULT_KV_SWAP_MAX_MB,
                    help="换页文件总字节配额（MB，LRU 淘汰最老的），默认 %(default)s。"
                         "32K 上下文下每段对话约 1.6GB，别把 tmpfs 写满")
    argv = sys.argv[1:]
    if "--" not in argv:
        ap.error("backend command must follow '--', e.g. "
                 "-- python3 ... -- <demo argv>")
    cut = argv.index("--")
    args = ap.parse_args(argv[:cut])
    demo_argv = argv[cut + 1:]
    if not demo_argv:
        ap.error("empty backend command after '--'")
    _VERBOSE = bool(args.verbose)
    _TOOL_OUTPUT_MAX_BYTES = args.tool_output_max_bytes
    _TOOL_STABLE = not args.no_tool_stable

    backend = Backend(demo_argv, args.backend_log,
                      frame_fd="stdout" if args.frames_stdout else None)
    print("[gateway] starting backend, model load takes ~230-240s ...", flush=True)
    try:
        backend.start()
    except BackendError as exc:
        print("[gateway] backend start failed: %s" % exc, file=sys.stderr)
        return 2
    print("[gateway] backend ready: sessions=%d default_max_new_tokens=%d"
          % (backend.sessions, backend.default_max_new_tokens), flush=True)
    if not backend.protocol_v2:
        print("[gateway] 警告：后端不认识 COUNT/SAVE/LOAD 新帧（多半是旧二进制没重编）——"
              "窗口滚动与会话换页已自动关闭，退回旧行为。"
              "重编后端：bash scripts/build_serve.sh", file=sys.stderr, flush=True)
    if args.sessions and backend.sessions != args.sessions:
        print("[gateway] warning: --sessions %d but backend reports %d"
              % (args.sessions, backend.sessions), file=sys.stderr)

    pool = SessionPool(backend, idle_ttl=args.idle_ttl,
                       queue_timeout=args.queue_timeout,
                       contend_idle=args.contend_idle,
                       kv_swap_dir=args.kv_swap_dir,
                       kv_swap_cap=args.kv_swap_cap,
                       kv_swap_max_mb=args.kv_swap_max_mb,
                       sched_policy=args.sched_policy,
                       sched_weights=args.sched_weights,
                       sched_long_tokens=args.sched_long_tokens,
                       sched_long_max=args.sched_long_max,
                       sched_cost=args.sched_cost)
    print("[gateway] 会话调度: 最多 %d 段对话同时活跃，多的排队；"
          "静默 %.0fs 回收（**有人在等时**降到 %.0fs，只收最闲的一段），"
          "排队 %.0fs 未轮到则返回 503"
          % (backend.sessions, pool.idle_ttl, pool.contend_idle,
             pool.queue_timeout), flush=True)
    if pool.sched_policy != "fcfs":
        print("[gateway] 分配策略: %s · 成本口径=%s（长任务阈值=%d tok 并发上限=%d；策略表见 "
              "gateway/mlfq_sched.py）"
              % (pool.sched_policy, pool.sched_cost, int(pool.sched_long_tokens),
                 pool.sched_long_max),
              flush=True)
    else:
        print("[gateway] 分配策略: fcfs（先到先得，与改造前一致）", flush=True)
    try:
        if args.selftest:
            return selftest(backend, pool)

        Handler.gateway = Gateway(backend, pool, verbose=args.verbose,
                                  cors_origin=args.cors_origin,
                                  temp_file=args.temp_file,
                                  window_enable=not args.no_window,
                                  ctx_reserve=args.ctx_reserve,
                                  window_min_messages=args.window_min_messages)
        # 卡温采集服务在不在，开服的时候就说一句：不在的话页面上那一栏永远是 "—"，
        # 与其让人对着屏幕猜，不如在启动横幅里写清楚（和 CORS 那条同一个道理）。
        print("[gateway] RK1828 结温: %s"
              % ("%s 存在，读它" % args.temp_file if os.path.exists(args.temp_file)
                 else "%s 不存在 —— 页面上卡温会显示 \"—\"。"
                      "装上采集服务：见 rknn-temp.service 顶上的说明" % args.temp_file),
              flush=True)
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        httpd.daemon_threads = True
        print("[gateway] listening on http://%s:%d  (sessions=%d, model=%s)"
              % (args.host, args.port, backend.sessions, MODEL_ID), flush=True)
        # 把"跨源放行"这件事在启动横幅里说清楚：它是**默认关闭**的，关了以后
        # 谁想跨源来用（哪怕是把自己的演示页存到本地双击打开）都会失败，
        # 到时候翻日志能一眼看到"哦，是没配 --cors-origin"，而不是去怀疑网络。
        print("[gateway] 跨源(CORS): %s"
              % ("放行 %s（其余来源一律拒绝）" % args.cors_origin if args.cors_origin
                 else "全部拒绝（同源的 /demo 不受影响；要放行请给 --cors-origin）"),
              flush=True)
        # 别再写死"4 个对话框"：页面 2026-09-20 起按 /health 的 sessions 决定开几个窗口
        # （32768 那份导出只够 2 路），屏幕上看的是几个就是几个。ctx_size 也一并印出来，
        # 因为它才是"为什么只有这么几个窗口"的答案。
        print("[gateway] 演示页（窗口数 = 本后端的会话数，当前 %d 个；上下文 %s）: "
              "http://%s:%d/demo"
              % (backend.sessions,
                 backend.ctx_size if backend.ctx_size is not None else "未在命令行给出",
                 args.host, args.port), flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
