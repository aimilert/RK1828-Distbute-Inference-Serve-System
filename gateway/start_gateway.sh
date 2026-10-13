#!/bin/bash
# 板卡上启动 OpenAI 网关。网关是父进程，自己拉起 rknn_multicard_demo --serve 作为
# 子进程（用管道把帧通道 fd 交给它），所以只有一个进程要管。
#
# 为什么不用官方 rkllm3-server：它的 OpenAI 接口是现成的，但文档 §4.5.2 写明
# 「推理执行排队串行」，实测 --n-session 1/2/4 聚合都是 ~11 tok/s（1.03x）——slot
# 只提供隔离、不提供并行。我们自己的执行器在同一硬件上是 3.33x，所以 HTTP 自己写。
#
# 命令行沿用验收时用的那一套（taskset f0 + LD_LIBRARY_PATH=./lib +
# --core-mask 0xff --stage-count 4 --bucket-size 128 --ctx-size 4096），不要改动，
# 否则实测数字不再可比。多会话方案的实测记录与全部已知限制见本目录的 CHANGELOG.md
# 与 README.md（含与官方 rkllm3-server 3.33x vs 1.03x 的对照）。
#
# 环境变量：
#   INSTALL_DIR  安装目录（含 rknn_multicard_demo 与 lib/）；默认取仓库里的默认安装路径
#   MODEL_DIR    模型目录（*.rknn / *.weight / *.tokenizer.gguf / *.embed.bin / *.safetensors）
#   MODEL_PREFIX 权重/分词器文件名前缀；默认 Qwen3.5-27B-llm（换一批模型时改这个即可）
#   MODEL_ID     /health、/v1/models 里展示的模型名；默认 qwen3.5-27b（换模型时建议同步改）
#   DUMP_REQ     设 1 时把每次请求的**摘要**追加到 $GATEWAY_DIR/request_dump.jsonl（调试用，
#                默认关）；可用 DUMP_REQ_FILE / DUMP_REQ_CHARS 改路径与预览长度。
#   PARALLEL_LOAD 设 1 时执行器**四卡并行加载**（默认串行；`0`/`false`/`no` 视为关）。
#   LOAD_TIMING  设 1 时打印每卡加载拆解与初始化循环 wall（同上，`0`/`false`/`no` 视为关）。
#   GATEWAY_DIR  本脚本与网关所在目录；默认 = 脚本自己所在目录
#   B            后端二进制名；默认 ./rknn_multicard_demo.serve（由 build_serve.sh 产出）
#   NSESSION     会话数 = **同时活跃的对话数上限**；默认 4。真实上限由内存账决定
#                （KV 按"每路满上下文"预分配 ⇒ 上限 ≈ 可用 KV / (上下文 × 每 token 开销)）：
#                **4096/8192 导出实测 5 路封顶，32768 导出只够 2 路**。下面那道容量闸会
#                按模型算一遍，算出来不够就直接拒绝启动（要硬上就 FORCE_SESSIONS=1）。
#   IDLE_TTL     一段对话静默超过这么多秒就把它占的会话收回给排队者；默认 300（0=不回收）
#   CONTEND_IDLE **已经有请求非等不可**时用的静默阈值（只收最闲的那一段对话）；默认 15。
#                没有它，第 N+1 段对话要等到某一段静默满 IDLE_TTL——那段时间里 NPU 是
#                真空着的，用户看到的就是"没人用，我却要等"。0 = 关掉这一级。
#   QUEUE_TIMEOUT 取不到会话时最多排队等这么多秒，超了返回 503；默认 600（要 > IDLE_TTL）
#   PORT         监听端口；默认 18280
#   NP           每轮 max_new_tokens 的进程默认值；默认 512
#   CTX          上下文长度（= --ctx-size）；默认 4096。⚠️ **它是一个"请求"，会在模型
#                编进去的那一组 KV 候选里被夹住**——权威答案是 SDK 日志里的
#                `chosen kvcache_buffer_lens`，不是这个数。两种夹法都见过：
#                  · 老 4096 导出 + `--ctx-size 8192` → 挑到 4096（上不去，四个 stage
#                    仍打 `max_ctx_len=4096`）；
#                  · **32768 导出 + `--ctx-size 8192` → 仍然挑 32768**（一份导出只有
#                    一档 KV：`n_lens=1`）⇒ **拿 32768 的导出想省内存是省不掉的**，
#                    只能换一份更小的导出。
#                另外 `--ctx-size` 也提不上去 `max_ctx_len`（那个来自
#                `rknn3_query(RKNN3_QUERY_LLM_CONFIG)`，是模型编译进去的）。
#                核模型落在哪一档用 rope 外置张量的大小最快：每个位置 256 字节 + 432
#                字节头 ⇒ 8192 → 2.0 MiB、32768 → 8.0 MiB；形状是 [1,4,1,N,16]。
#   FORCE_SESSIONS 设成 1 就跳过下面那道容量闸（默认 0）。只在明确知道后果时用：
#                超了不是报错，是把四张卡压死到要重启板卡。
#   HOST         监听地址；默认 0.0.0.0 = 局域网可达
#   LOG          后端日志路径；默认 $GATEWAY_DIR/gateway_backend.log
#
# 三个改动的开关（都可选，默认值即推荐用法）：
#   WINDOW=0     关闭窗口滚动（改动 A）。默认开启：prompt 超上下文预算时保 system + 近期、
#                丢最老历史，让单会话一直聊下去，而不是攒长了就 HTTP 400。
#   TOOL_MAX_BYTES 单条 tool 结果字节上限（改动 B）；默认 8192，0 = 关闭截断。
#   KV_SWAP_DIR  会话换页目录（改动 C）；不设 = 关闭。设了（建议 tmpfs，如
#                /dev/shm/rknn-kv）后，被回收/让出的对话会把 KV 存盘，同一段对话回来时
#                读回，免去一次全量 prefill。
#
# 关于 HOST：默认 0.0.0.0 是为了让工作流 Agent 能从别的机器/容器连进来（官方 server
# 也是这么暴露的）。如果 Agent 就跑在板卡本机，把它设成 127.0.0.1 更稳妥——这个网关
# 没有任何鉴权，谁能连上谁就能用满 4 张卡的算力。
#
# 关于 PORT（2026-09-20 从 8080 改成 18280）：**换端口本身不是安全控制**——同一个
# 局域网里扫一遍就找到了，真正的门禁是 CORS（见下）和"要不要加鉴权"。改的是把这块
# 板卡从"默认端口那一份名单"上挪开：8080 是几乎每个端口扫描器第一个试的，而这个服务
# 没有任何鉴权，被扫到就等于被用上。想改回来 `PORT=8080 ./start_gateway.sh` 即可，
# 但**所有客户端（同事的隧道、cc-switch 的 Base URL、书签）都得跟着改**。
#
#   CORS_ORIGIN  允许哪个**来源**跨源调用（浏览器那道门禁）。**默认空 = 一个都不放行**，
#                演示页是同源的、用不到它。要放开就写全 `scheme://host[:port]`。
#
# 用法（板上）：
#   INSTALL_DIR=.../install/rknn_multicard_demo \
#   MODEL_DIR=.../Qwen3.5-27B ./start_gateway.sh
set -u
GATEWAY_DIR=${GATEWAY_DIR:-$(cd "$(dirname "$0")" && pwd)}
# 默认按项目根目录的相对位置找：`build_serve.sh` / `cmake --install` 的安装产物。
REPO_ROOT=${REPO_ROOT:-$(cd "$GATEWAY_DIR/.." && pwd)}
INSTALL_DIR=${INSTALL_DIR:-$REPO_ROOT/install/rknn_multicard_demo}
MODEL_DIR=${MODEL_DIR:-$REPO_ROOT/Qwen3.5-27B}
MODEL_PREFIX=${MODEL_PREFIX:-Qwen3.5-27B-llm}   # 权重/分词器文件名前缀（换模型时改这个，不必改脚本）
B=${B:-./rknn_multicard_demo.serve}
NSESSION=${NSESSION:-4}
IDLE_TTL=${IDLE_TTL:-300}
CONTEND_IDLE=${CONTEND_IDLE:-15}
QUEUE_TIMEOUT=${QUEUE_TIMEOUT:-600}
PORT=${PORT:-18280}
NP=${NP:-512}
CTX=${CTX:-4096}
HOST=${HOST:-0.0.0.0}
LOG=${LOG:-$GATEWAY_DIR/gateway_backend.log}
FORCE_SESSIONS=${FORCE_SESSIONS:-0}
# 会话分配策略（2026-09-29 加）：fcfs（默认，与改造前一致）| mlfq（端侧五因子调度）。
# 只在"会话空出来、队列里不止一个人等"时起作用；见 gateway/mlfq_sched.py。
SCHED_POLICY=${SCHED_POLICY:-fcfs}
SCHED_WEIGHTS=${SCHED_WEIGHTS:-}          # 空 = 网关默认 (0.4,0.3,0.15,0.1,0.05)
SCHED_LONG_TOKENS=${SCHED_LONG_TOKENS:-}  # 空 = 网关默认 8192
SCHED_LONG_MAX=${SCHED_LONG_MAX:-}        # 空 = max(1, NSESSION//2)

# ---- 相对路径先转绝对路径（2026-09-21 修）----
# 为什么必须在 cd 之前做：下面要 cd 到 $INSTALL_DIR，而 cd 之后**相对路径的参照系
# 就换成了安装目录**——`MODEL_DIR=../../xyf_workspace/...` 这种写法在 cd 前能找到
# 模型（容量闸那一行用的就是它），cd 后再检查同一个字符串就变成"no such model dir"。
# 一次调用里同一个变量被解析出两个不同的位置，症状极具误导性。转成绝对路径之后，
# 从哪个目录调用、脚本内部 cd 到哪里，都指向同一个地方。
_abspath() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$PWD" "$1" ;; esac; }
INSTALL_DIR=$(_abspath "$INSTALL_DIR")
MODEL_DIR=$(_abspath "$MODEL_DIR")
LOG=$(_abspath "$LOG")

# ---- 容量闸：上下文 × 路数超了就不往下走（2026-09-20 加）----
#
# 为什么必须有：KV 是**在 session_init 时按"每路满上下文"一次性预分配**的，所以内存账
# 就是"上下文 × 路数"，跟这一路实际用掉多少 token 无关。超了之后官方那半截**不报错**：
# `rknn3_session_init` 卡在 `wait_event(0x100000): timed out`，那个端点从此不应答，之后
# 每次加载都在 `rknn3_init` 上 `ERROR_PIPE`；`systemctl restart rknn3` 也救不回来
# （四个 rknn3_transfer_proxy 一起没了，`ddr load addr request failed` / 退出码 234）
# ——**只能重启板卡**。2026-09-20 就是这么撞的：32768 那份导出（每路 406.7 MB/卡，
# 每卡只够 2 路）配默认的 NSESSION=4，四张卡全僵死。
# 现场最坑的一点是默认值：CTX 和 NSESSION 各自看着都合理，**组合起来才致命**。
#
# 判据从 rope 外置张量的大小反推上下文 N，不解析 JSON：每个位置 256 字节 + 432 字节头
# （8192 → 2097584 B、32768 → 8389040 B，实测）。读不到就**不拦**（可能是别的命名/别的
# 模型），只在能算出来的时候说话。
ROPE=${ROPE:-$MODEL_DIR/${MODEL_PREFIX}_seg0.safetensors}
MAXSESS=""
if [ -f "$ROPE" ]; then
  BYTES=$(stat -c%s "$ROPE" 2>/dev/null || echo 0)
  if [ "$BYTES" -gt 432 ] && [ $(( (BYTES - 432) % 256 )) -eq 0 ]; then
    NMODEL=$(( (BYTES - 432) / 256 ))
    # 每路 KV ≈ N × 12.4 KB/卡（实测 32768 → 406.7 MB/卡 ⇒ 0.0124 MB/token）。
    # 分给 KV 的额度 ≈ 1078 MB/卡 = 5071（卡上总量）− 3993（权重+internal，stage3 最高）。
    # 再压一个 5 路的天花板：P0 时代 4096/8192 导出实测就是 5 路封顶（KV 之外还有
    # 每路固定的激活/工作缓冲）。这条线性式能同时对上三个实测点：
    # 4096→5、8192→5、32768→**2**。
    PER=$(( NMODEL * 12412 / 1000000 ))          # MB/卡/路
    [ "$PER" -lt 1 ] && PER=1
    MAXSESS=$(( 1078 / PER ))
    [ "$MAXSESS" -gt 5 ] && MAXSESS=5
    echo "[容量] 这份导出的上下文 = $NMODEL，每路 KV ≈ $PER MB/卡 ⇒ 最多 $MAXSESS 路会话"
    if [ "$NSESSION" -gt "$MAXSESS" ] && [ "$FORCE_SESSIONS" != "1" ]; then
      cat <<EOF >&2
拒绝启动：这份导出最多 $MAXSESS 路会话，而 NSESSION=$NSESSION。

  上下文 $NMODEL（从 $(basename "$ROPE") 的大小反推）
  每路 KV ≈ $PER MB/卡，可给 KV 的 ≈ 1078 MB/卡（权重+internal 已占 3993，卡上共 5071）

超了**不会报错**：rknn3_session_init 卡在 wait_event 超时 → 端点不再应答 → 之后每次
加载都是 ERROR_PIPE → systemctl restart rknn3 也救不回来 → **只能重启板卡**。

改小重来：  NSESSION=$MAXSESS $0 ...
已知风险仍要起：FORCE_SESSIONS=1 NSESSION=$NSESSION $0 ...
（想先量准数，用 --probe-sessions N 探路径，别拿默认值直接压上去。
  实测记录见 CHANGELOG.md 的 2026-09-20 那一条）
EOF
      exit 1
    fi
  fi
fi

cd "$INSTALL_DIR" || { echo "cannot cd $INSTALL_DIR (set INSTALL_DIR)"; exit 1; }
[ -d "$MODEL_DIR" ] || { echo "no such model dir: $MODEL_DIR (set MODEL_DIR)"; exit 1; }
export LD_LIBRARY_PATH=./lib

# CORS_ORIGIN 只在设了的时候才把那个开关递下去：不设就**一个跨源请求都不放行**
# （网关那边的默认行为），命令行上也就不出现这个参数——省得看进程表的人以为放行了谁。
CORS_ARG=""
[ -n "${CORS_ORIGIN:-}" ] && CORS_ARG="--cors-origin $CORS_ORIGIN"

# 改动 C：会话换页目录（可选）。设了才把 KV 存到盘上、同一段对话回来时读回；不设就
# 保持"不落盘"的原行为。建议指向 tmpfs（如 /dev/shm/rknn-kv）以免写闪存。
KV_SWAP_ARG=""
[ -n "${KV_SWAP_DIR:-}" ] && KV_SWAP_ARG="--kv-swap-dir $KV_SWAP_DIR"
[ -n "${KV_SWAP_CAP:-}" ] && KV_SWAP_ARG="$KV_SWAP_ARG --kv-swap-cap $KV_SWAP_CAP"
[ -n "${KV_SWAP_MAX_MB:-}" ] && KV_SWAP_ARG="$KV_SWAP_ARG --kv-swap-max-mb $KV_SWAP_MAX_MB"

# 实验开关（默认关）：KV_SINGLE_SEG=N 时 KV 存/取**只对 stage N** 生效（A4 实验用）。
KV_SINGLE_SEG_ARG=""
[ -n "${KV_SINGLE_SEG:-}" ] && KV_SINGLE_SEG_ARG="--kv-single-seg $KV_SINGLE_SEG"

# 改动 B：单条 tool 结果的字节上限（可选，网关默认 8192）。设 0 = 关闭截断。
TOOL_ARG=""
[ -n "${TOOL_MAX_BYTES:-}" ] && TOOL_ARG="--tool-output-max-bytes $TOOL_MAX_BYTES"

# 改动 A：窗口滚动默认开启；WINDOW=0 可关掉（退回"超预算就 400"的旧行为，便于对比）。
WINDOW_ARG=""
[ "${WINDOW:-1}" = "0" ] && WINDOW_ARG="--no-window"

# 调度策略：fcfs 也显式传（进程表上一眼能看出跑的是哪种），其余三个只有设了才传。
SCHED_ARG="--sched-policy $SCHED_POLICY"
[ -n "${SCHED_WEIGHTS:-}" ] && SCHED_ARG="$SCHED_ARG --sched-weights $SCHED_WEIGHTS"
[ -n "${SCHED_COST:-}" ] && SCHED_ARG="$SCHED_ARG --sched-cost $SCHED_COST"
[ -n "${SCHED_LONG_TOKENS:-}" ] && SCHED_ARG="$SCHED_ARG --sched-long-tokens $SCHED_LONG_TOKENS"
[ -n "${SCHED_LONG_MAX:-}" ] && SCHED_ARG="$SCHED_ARG --sched-long-max $SCHED_LONG_MAX"

exec taskset f0 python3 "$GATEWAY_DIR/rkllm_gateway.py" \
  --host "$HOST" --port "$PORT" \
  $CORS_ARG $KV_SWAP_ARG $TOOL_ARG $WINDOW_ARG $SCHED_ARG \
  --sessions "$NSESSION" --verbose \
  --idle-ttl "$IDLE_TTL" --contend-idle "$CONTEND_IDLE" \
  --queue-timeout "$QUEUE_TIMEOUT" \
  --backend-log "$LOG" \
  -- "$B" \
     --model "$MODEL_DIR/${MODEL_PREFIX}_seg0.rknn" \
     --weight "$MODEL_DIR/${MODEL_PREFIX}_seg0.weight" \
     --vocab "$MODEL_DIR/${MODEL_PREFIX}.tokenizer.gguf" \
     --embed "$MODEL_DIR/${MODEL_PREFIX}.embed.bin" \
     --ctx-size "$CTX" --core-mask 0xff --stage-count 4 --bucket-size 128 \
     --rope-tensor "$MODEL_DIR/${MODEL_PREFIX}_seg0.safetensors" \
     --sessions "$NSESSION" --serve -n "$NP" $KV_SINGLE_SEG_ARG
