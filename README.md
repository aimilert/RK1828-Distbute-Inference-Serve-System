# rk182X-multisession

> 多路并发推理 · Qwen3.5-27B · RK1828 ×4

在 **4 片 RK1828** 上流水线并行地跑 **Qwen3.5-27B**，对外提供一个**兼容 OpenAI** 的
HTTP 网关，让多路会话真正并行地流式出字。

本项目是一个面向多路并发推理的独立服务：按多路并发服务所需组织代码，不包含模型导出
脚本与外部设计文档，可以直接编译、部署和运行。

一句话版本：官方 `rkllm3-server` 的 OpenAI 接口是现成的，但按设计「推理执行排队串行」
（同板实测 1.03×）；本项目的网关复用自己的执行器，同板同条件 **3.33×**
（连量 4 轮 3.32~3.38）。

## 组成


| 目录              | 内容                                                                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/`            | 多会话并发执行器 `main.cc`（`--serve` / `--serve-fd` 帧协议），也是多卡流水线的推理核心                                                               |
| `include/`        | 编译该执行器所需的自带头文件：`rknn3_api.h`、`float16.h`、`Tokenizer.h`、`nlohmann/json`                                                             |
| `third_party/`    | 瑞芯微预编译库（Linux）：`librknn3_api.so`、`librknn3_api_rkcp.so`、`librknn3_api_native.so`、`librga.so`、`libtokenizer.a`（`aarch64` + `x86_64`） |
| `gateway/`        | 板端 HTTP 网关与运行时组件（`rkllm_gateway.py`、`mlfq_sched.py`、`toolcalls.py`、演示页、启动脚本）                                                   |
| `scripts/`        | 构建脚本（`build_serve.sh` / `build_serve_native.sh`；带产物断言）                                                                                    |
| `tests/`          | 验收与自检脚本（`run_all_board_tests.sh`、`page_check.js`、`fake_backend.py` 等）                                                                     |
| `tools/`          | 基准与运维工具（`bench_agent.py`、`benefit_eval.py`、`rknn_smi_temp.py`、演示脚本）                                                                  |
| `docs/`           | 文档：`USAGE.md`（用法）/ `DEMO.md`（演示）/ `CHANGELOG.md`（变更与实测口径）/ `PERF.md`（指标口径）/ `Claude接入RK1828本地模型API指南.md`（接入 how-to） |
| `algorithm/mlfq/` | 上层调度算法的服务端参考实现                                                                                                                          |
| `CMakeLists.txt`  | 独立构建入口                                                                                                                                          |

执行器与网关的关系：

```
浏览器 / Agent
      │  HTTP (OpenAI 兼容)
      ▼
gateway/rkllm_gateway.py        ← 会话池、调度、SSE、/demo 演示页
      │  帧协议（管道 fd）
      ▼
rknn_multicard_demo.serve       ← src/main.cc 编译产物，多会话并发执行器
      │  RKNN3 runtime
      ▼
RK1828 ×4（Qwen3.5-27B 按层切成 4 段，每卡一段）
```

## 依赖

编译所需的瑞芯微库与头文件**已随仓库带在 `third_party/` 与 `include/` 下**（Linux
`aarch64` / `x86_64`）：`librknn3_api.so`、`librknn3_api_rkcp.so`、
`librknn3_api_native.so`、`librga.so`、`libtokenizer.a`。默认构建直接取用，无需再设置
`LIBRKNNAPI` / `LIBTOKENIZER`；只有在使用**另一份 SDK 构建**时才需要覆盖：

```bash
LIBRKNNAPI=/path/to/librknn3_api.so LIBTOKENIZER=/path/to/libtokenizer.a bash scripts/build_serve.sh
```

仍然需要自行准备的是**模型文件**（体积大，不随仓库分发）：分段模型
`*_seg0.rknn`、`*.weight`、`*.tokenizer.gguf`、`*.embed.bin`、`*_seg0.safetensors`
—— 由模型导出流程生成，运行时经 `MODEL_DIR` 指定。

## 编译

### 方式一：交叉编译（x86 构建机 → 板卡）

```bash
cd scripts
GCC_COMPILER=aarch64-linux-gnu bash build_serve.sh
```

（库默认取 `third_party/` 下的 `aarch64` 版本；用 `TARGET_ARCH=x86_64` 或
`LIBRKNNAPI=` / `LIBTOKENIZER=` 可覆盖。）

产物另存到项目根目录的 `rknn_multicard_demo.serve`，同时按 `$ORIGIN/lib` 的 rpath
把 SDK 库装进 `install/rknn_multicard_demo/lib/`。

### 方式二：板卡上原生编译

```bash
cd scripts
bash build_serve_native.sh
```

（默认把 `third_party/` 下的运行库铺到 `install/rknn_multicard_demo/lib/`，并就地编译、
安装后端二进制。）

### 方式三：直接调用 CMake

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
cmake --install build
```

（默认 `TARGET_ARCH=aarch64`；宿主机自测用 `-DTARGET_ARCH=x86_64`。）

> 两个 `build_serve*.sh` 都带**产物断言**（时间戳、无 sanitizer、关键字面量），
> 避免"编译失败却装上了旧二进制"。

## 部署与运行

以下命令**在仓库根目录执行**（`$PWD` 与下载位置无关）。

1. 把构建产物与网关文件放在板卡的**同一个目录**（网关按自身目录读演示页）：
   `rknn_multicard_demo.serve`、`gateway/rkllm_gateway.py`、`gateway/start_gateway.sh`、
   `gateway/restart_gateway.sh`、`gateway/demo_4chat.html`。
2. 启动（模型加载约 2 分钟（串行）/ 约 1 分钟（`PARALLEL_LOAD=1`）；`MODEL_DIR` 指模型导出目录，含 `*.rknn` / `*.weight` /
   `*.tokenizer.gguf` / `*.embed.bin`）：

```bash
INSTALL_DIR=$PWD/install/rknn_multicard_demo \
MODEL_DIR=<模型目录>/Qwen3.5-27B-CTX32768 \
NSESSION=2 CTX=32768 WINDOW=0 SCHED_POLICY=fcfs PARALLEL_LOAD=1 \
bash gateway/restart_gateway.sh
```

3. 浏览器打开 `http://<板卡>:18280/demo`，或调 `/v1/chat/completions`。

两种常用导出：

```bash
# 32K 导出：2 路会话
MODEL_DIR=<模型目录>/Qwen3.5-27B-CTX32768 NSESSION=2 CTX=32768 ... PARALLEL_LOAD=1 bash gateway/restart_gateway.sh
# 8K 导出：4 路会话 + KV 换页
MODEL_DIR=<模型目录>/Qwen3.5-27B-CTX8192  NSESSION=4 CTX=8192 KV_SWAP_DIR=/dev/shm/rknn-kv ... PARALLEL_LOAD=1 bash gateway/restart_gateway.sh
```

- **`PARALLEL_LOAD=1`**：四卡并行加载（默认串行；`0`/`false`/`no` 视为关）。实测就绪
  **134 s → 62 s**（受磁盘读带宽上限约束）；设 `LOAD_TIMING=1` 可打印每卡加载拆解。

### 停止服务

```bash
# 停网关（网关是父进程，退出时会带走后端子进程）
pkill -f 'rkllm_gateway[.]py'

# 保险：清掉"网关非正常死亡（SIGKILL/崩溃）"留下的孤儿后端（否则四张卡还被占着）
pkill -f 'rknn_multicard_demo[.]serve'

# 确认已停：下面两条都应无输出
pgrep -af 'rkllm_gateway'
pgrep -af 'rknn_multicard_demo'
```

- **`[.]` 不是笔误**：`pkill -f` 会匹配**执行它的那条命令本身**（命令行里含 `rkllm_gateway.py`），
  从而把调用它的 shell 一起杀掉、后面命令全不执行；写成 `rkllm_gateway[.]py` 就不会自匹配。
- 用过 KV 换页（`KV_SWAP_DIR`）可顺手清缓存：`rm -rf /dev/shm/rknn-kv`。
- 装了卡温采集服务（可选）要停：`sudo systemctl disable --now rknn-temp`。
- 重新拉起：`bash gateway/restart_gateway.sh`（模型加载见上：串行 ~140s / `PARALLEL_LOAD=1` ~62s）。

### 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `INSTALL_DIR` | 仓库内默认安装路径 | 含后端二进制与 `lib/` |
| `MODEL_DIR` | 仓库内默认模型路径 | 模型目录 |
| `GATEWAY_DIR` | 脚本自身目录 | 网关与演示页所在目录 |
| `B` | `./rknn_multicard_demo.serve` | 后端二进制名 |
| `NSESSION` | 4 | 同时活跃的会话数上限（见下「NSESSION 与容量闸」） |
| `PORT` | 18280 | 监听端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `NP` | 512 | 每轮 `max_new_tokens` 默认值 |
| `CTX` | 4096 | 上下文长度**请求**（见下「CTX」） |
| `WINDOW` | 1（开） | 0 = 关窗口滚动（超预算不裁剪，退回 400） |
| `IDLE_TTL` | 300 | 静默这么久收回会话；**0 = 不回收（总开关）** |
| `CONTEND_IDLE` | 15 | "有人非等不可"时用的静态阈值；0 = 关这一级 |
| `QUEUE_TIMEOUT` | 600 | 排队上限，超了返回 503；要大于 `IDLE_TTL` |
| `SCHED_POLICY` | `fcfs` | 会话分配策略 |
| `SCHED_COST` / `SCHED_WEIGHTS` / `SCHED_LONG_TOKENS` / `SCHED_LONG_MAX` | — | 调度可调项 |
| `KV_SWAP_DIR` / `KV_SWAP_CAP` / `KV_SWAP_MAX_MB` | 关 | 会话 KV 换页 |
| `PARALLEL_LOAD` / `LOAD_TIMING` | 关 | 四卡并行加载 / 打印加载拆解 |
| `MODEL_PREFIX` | `Qwen3.5-27B-llm` | 权重/分词器文件名前缀 |
| `MODEL_ID` | `qwen3.5-27b` | `/health`、`/v1/models` 展示名 |
| `DUMP_REQ` | 关 | 设 1 把每次请求摘要写 `gateway/request_dump.jsonl` |
| `CORS_ORIGIN` | 空（不放行） | 放行**一个**跨源来源（见 `docs/USAGE.md`「已知限制」） |
| `FORCE_SESSIONS` | 0 | 设 1 跳过容量闸（危险，见下「NSESSION 与容量闸」） |

### `CTX` 是一个"请求"，不是开关

`CTX` 只能在模型编进去的那组 KV 候选里挑；权威答案是后端日志的 `chosen kvcache_buffer_lens`。
**32768 导出请求 8192 仍会拿到 32768**（一份导出只有一档 KV，`n_lens=1`）⇒ 想省内存只能换更小的导出。
判断是哪一档最快的办法是看 rope 外置张量大小：每位置 256 B + 432 B 头 ⇒ 8192 → 2.0 MiB、32768 → 8.0 MiB。

### `NSESSION` 与容量闸

- KV 在**会话建出来时按满上下文一次性预分配**，账是「上下文 × 路数」。实测封顶：
  **4096/8192 导出 5 路**（第 6 个在 stage0 起不来）；**32768 导出 2 路**（每路 406.7 MB/卡）。
- 会话**只在启动时**创建，运行时不新建 ⇒ "同时能服务多少人" = `NSESSION`，多出来的人排队。
- ⚠️ **超开不报错，而是把四张卡压死**：`rknn3_session_init` 卡在 `wait_event` 超时 → 端点不再应答 →
  之后每次加载 `ERROR_PIPE` → `systemctl restart rknn3` 也救不回来 ⇒ **只能重启板卡**。
  因此 `start_gateway.sh` 会从 rope 张量反推上下文、与 `NSESSION` 比，超了**拒绝启动**；硬上需 `FORCE_SESSIONS=1`。

其余（接口、会话调度、卡温采集、已知限制、测试）见 [`docs/USAGE.md`](docs/USAGE.md)；
演示流程见 [`docs/DEMO.md`](docs/DEMO.md)。

## 注意事项

- 网关**无鉴权**、默认监听 `0.0.0.0`：只在可信局域网内运行，能连上的人就能用满 4 张卡的算力。
- `NSESSION` 的上限由**内存账**（上下文 × 路数）决定，不是固定值；超开会把四张卡压死到需要
  重启板卡，`start_gateway.sh` 会在启动前拦截。
- 完整限制见 [`docs/USAGE.md`](docs/USAGE.md) §6。

## 许可

本项目遵循 Apache License 2.0（见 [`LICENSE`](LICENSE)）。
