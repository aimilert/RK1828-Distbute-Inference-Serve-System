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
| `docs/`           | 面向使用者的文档（`README.md` / `DEMO.md` / `CHANGELOG.md` / `PERF.md`）                                                                             |
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

1. 把 `rknn_multicard_demo.serve`、`gateway/rkllm_gateway.py`、`gateway/start_gateway.sh`、
   `gateway/restart_gateway.sh`、`gateway/demo_4chat.html` 放在板卡的**同一个目录**
   （网关按自身目录读取演示页）。
2. 按 `docs/README.md` 设置 `INSTALL_DIR` / `MODEL_DIR` 并启动：

```bash
cd "$GATEWAY_DIR"
INSTALL_DIR=../install/rknn_multicard_demo MODEL_DIR=../../xyf_workspace/4rk1828_Test/Qwen3.5-27B/ NSESSION=2 ./start_gateway.sh
```

3. 浏览器打开 `http://<板卡>:18280/demo`，或按 `docs/README.md` 调
   `/v1/chat/completions`。

**完整操作流程**（含每一步"观众该看到什么"）见
[`docs/DEMO.md`](docs/DEMO.md)；**接口、环境变量、已知限制**见
[`docs/README.md`](docs/README.md)；**变更与实测口径**见
[`docs/CHANGELOG.md`](docs/CHANGELOG.md)。  

## 注意事项

- 网关是**无鉴权**的常驻服务，默认监听 `0.0.0.0`。只应在可信局域网内运行；能连上的人
  就能用满 4 张卡的算力。
- 会话数 `NSESSION` 的上限由**内存账**（上下文 × 路数）决定，不是固定值。超开会把四张卡
  压死到需要重启板卡，`start_gateway.sh` 会在启动前拦截。
- 运行前请先读 `docs/README.md` 的「已知限制」一节。

## 许可

本项目遵循 Apache License 2.0（见 [`LICENSE`](LICENSE)）。
