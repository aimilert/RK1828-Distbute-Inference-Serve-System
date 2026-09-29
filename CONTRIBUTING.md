# 贡献指南（Contributing）

本仓库在 **RK3588 + 4×RK1828** 的边缘设备上提供多路并发大模型推理服务。改动前请先读
[`README.md`](README.md) 与 [`docs/README.md`](docs/README.md)，它们说明了系统的边界与
已知限制。

## 1. 项目结构

```
.
├── CMakeLists.txt          # 唯一构建入口
├── src/main.cc             # 多会话并发执行器（C++，多卡流水线推理核心）
├── include/                # 执行器所需头文件（自有 + SDK 头 + nlohmann/json，第三方许可随附）
├── third_party/            # 厂商预编译库（aarch64 / x86_64）
├── gateway/                # 板端 HTTP 网关与运行时组件（python3 标准库，无第三方依赖）
│   ├── rkllm_gateway.py    #   网关本体（会话池、调度、SSE、工具调用、资源快照）
│   ├── mlfq_sched.py       #   端侧 MLFQ 调度内核（纯逻辑，默认不启用）
│   ├── demo_4chat.html     #   网页演示页（必须与网关同目录：网关从自身目录读取）
│   ├── start_gateway.sh    #   启动脚本（参数走环境变量）
│   └── …                   #   其余运行时组件
├── scripts/                # 构建脚本（build_serve.sh / build_serve_native.sh）
├── tests/                  # 验收与自检（run_all_board_tests.sh、page_check.js、fake_backend.py 等）
├── tools/                  # 基准与运维工具（bench_agent.py、benefit_eval.py、演示脚本）
├── algorithm/mlfq/         # 上层调度算法的实验实现（服务端参考实现）
└── docs/                   # 面向使用者的文档
```

## 2. 环境要求

| 用途 | 要求 |
|---|---|
| 板卡运行 | Linux（aarch64）、python3、RKNN3 Runtime；模型目录（分段导出，不随仓库分发） |
| 板卡原生编译 | `g++`（C++11）、`readelf`、`strings`、`md5sum` |
| 构建机交叉编译 | CMake ≥ 3.16、`aarch64-linux-gnu-` 工具链 |
| 本地回归（无板卡） | python3（标准库）；网页检查另需 Node.js |

## 3. 构建

```bash
# 方式一：构建机交叉编译（产物另存为仓库根 rknn_multicard_demo.serve）
cd scripts && GCC_COMPILER=aarch64-linux-gnu bash build_serve.sh

# 方式二：板卡上原生编译
cd scripts && bash build_serve_native.sh

# 方式三：直接调用 CMake
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && cmake --install build
```

两个 `build_serve*.sh` 都带**产物断言**（时间戳、不得链接 sanitizer、关键字面量），请勿
绕过它们；断言失败说明"装的不是这一份代码"。

> ⚠️ 行尾：仓库统一 LF（见 [`.gitattributes`](.gitattributes)）。**不要把带 CRLF 的文件
> 直接传到板卡**——历史上因此让 shell 脚本与 systemd 起不来（见 `docs/CHANGELOG.md`）。

## 4. 运行与验证

```bash
# 启动网关（模型加载约 4 分钟；路径按实际填）
cd gateway
INSTALL_DIR=… NSESSION=2 MODEL_DIR=… bash start_gateway.sh

# 单测/自检（不需要板卡的部分）
python3 gateway/rkllm_gateway.py --frames-stdout --selftest -- python3 tests/fake_backend.py
python3 tests/check_template.py

# 板卡全量验收 + 网页检查
tests/run_all_board_tests.sh http://127.0.0.1:18280
node tests/page_check.js http://<板卡>:18280

# 性能基准（TTFT/prefill/decode/排队/气泡比）
python3 tools/bench_agent.py --base http://127.0.0.1:18280 --label baseline --tools
```

改动网关行为时，请同时更新 `docs/CHANGELOG.md`（症状 → 根因 → 修法）与相关文档。

## 5. 代码风格

- **C++**：遵循 [`.clang-format`](.clang-format)（基线 Google C++ Style，仅花括号与列宽两处
  按项目既有习惯偏离）。只对改动过的文件跑 `clang-format -i`，不要全仓重排。
- **Python**：PEP 8，4 空格缩进；**仅用标准库**（网关不能引入第三方依赖，否则板卡要联网装包）。
- **Shell**：POSIX/`bash` 均可，`set -eu`；路径先转绝对路径再 `cd`（相对路径会在 `cd` 后改变语义）。
- **通用**：UTF-8、LF、文件末尾留一个换行；编辑器请遵守 [`.editorconfig`](.editorconfig)。
- 注释写"**为什么**"而不是"做了什么"；本项目大量踩坑结论都留在注释里，请保持这一风格。

## 6. 提交规范

提交信息用 `type(scope): 简述`，描述可用中文：

```
feat(gateway): 端侧 MLFQ 调度（五因子可观测近似 + 长任务限流）
fix(gateway): 超预算压缩工具（保可调用）+ 客户端断开即叫停后端
docs: 新增《…指南》
chore: 更新 .gitignore
```

- 一次提交只做一件事；行为变更必须带验证方式（测试命令或实测数据）。
- 分支命名：`feat/<主题>`、`fix/<主题>`、`chore/<主题>`；对外可引用的基线打 tag
  （如 `perf-baseline`、`mlfq-edge-v1`）。

## 7. 不要提交的内容

已在 [`.gitignore`](.gitignore) 中排除，请勿用 `-f` 强行加入：

- 模型与张量（`*.rknn`、`*.weight`、`*.gguf`、`*.embed.bin`、`*.safetensors`）；
- 构建/部署产物（`build/`、`install/`、`rknn_multicard_demo*`）与运行时日志；
- 内部资料：厂商 SDK 文档（`doc_CN/`）、测试数据与实测报告（`docs/*.xls`、`docs/Orin*`、
  `docs/Qwen3.5-27B-*`）、产品/公司材料、内部计划文档；
- 任何含密钥、内网地址、个人信息的文件。

## 8. 安全

网关**默认无鉴权**、监听 `0.0.0.0`。部署与上报安全问题的要求见 [`SECURITY.md`](SECURITY.md)。
