# 板端 OpenAI 兼容服务（多会话）

给工作流 Agent 用的 HTTP 接口，跑在板卡上，后端是 `src/main.cc --serve` 的**多会话并发执行器**。

> **要给人演示**（而不是自己用）：直接看 **[`DEMO.md`](DEMO.md)**——那是给第一次接触项目的人写的
> 完整操作流程，含每一步"观众该看到什么"和排障表。

**设计说明、实测数据与全部已知限制**见本目录的 [`CHANGELOG.md`](CHANGELOG.md)，
这里只讲怎么用。一句话版本：官方 `rkllm3-server` 的 OpenAI 接口是现成的，但按设计
「推理执行排队串行」（同板实测 1.03×）；这份门面复用自己的执行器，同板同条件 **3.33×**（连量 4 轮 3.32~3.38）。

## 文档地图

| 文档 | 面向 | 内容 |
|---|---|---|
| [`README.md`](README.md)（本文） | 使用者 | 起服务、接口、已知限制——**怎么用** |
| [`DEMO.md`](DEMO.md) | 演示者 | 从零到跑通一次 4 路并发演示的完整流程 |
| [`CHANGELOG.md`](CHANGELOG.md) | 开发者 | 按日期的变更与踩坑记录 |
| [`PERF.md`](PERF.md) | 评测者 | 指标口径（两层指标、两个吞吐口径）、`/v1/perf`、A/B 标准流程 |

## 组成

| 文件 | 作用 |
|---|---|
| **`DEMO.md`** | **演示手册**：给第一次接触项目的人，从零到跑通一次 4 路并发演示的完整流程 |
| **`demo_4chat.html`** | **网页演示**：4 个对话框并排，四路回答同时流式输出（网关通过 `GET /demo` 发出，无需额外 web 服务） |
| **`demo_4session.py`** | 命令行演示：4 行实时进度条一起往前爬（终端里用这个） |
| `rkllm_gateway.py` | 网关本体（python3 **标准库**，无第三方依赖）。父进程，自己拉起后端子进程 |
| `start_gateway.sh` | 启动网关（含后端子进程）；参数走环境变量 |
| `restart_gateway.sh` | 杀掉重启（含两个测试陷阱的注释，别改成命令行一行流） |
| `build_serve.sh` | 在构建机上交叉编译出 `rknn_multicard_demo.serve`（Release） |
| `run_all_board_tests.sh` | 板上跑完全部验收测试，失败项集中列在最后 |
| `toolcalls.py` | 工具调用（function calling）：prompt 侧的渲染 + 回复侧的解析，含自带自检 |
| `tool_golden.json` | 工具渲染的金标准：真 Jinja2 渲模型自己的 `tokenizer.chat_template` 得到的字节 |
| `check_template.py` | 网关渲染的 prompt 与 `main.cc` 模板 / 模型模板**逐字节**比对（见下） |
| `serve_http_test.py` | 接口面：health / models / 非流式 / SSE / 多轮 KV 复用 / 并发（`tools` 模式另有工具调用） |
| `toolcall_check.py` | **真模型**的工具调用：会不会调、调用的形状认不认得、回显后 KV 还粘不粘得住 |
| `sticky_check.py` | 粘性复用的**语义**正确性（暗号靠 KV 存活 + prefill 计数） |
| `nothink_check.py` | 关思考（`enable_thinking=false`）下的粘性复用回归 |
| `http_scaling.py` | HTTP 层并发伸缩（自研门面存在的唯一理由） |
| **`mlfq_sched.py`** | **端侧 MLFQ 调度内核**（纯逻辑）：会话空出来时对排队者按五因子打分（因子全用可观测近似），`SCHED_POLICY=mlfq` 才启用；默认 `fcfs` 行为不变 |
| **`bench_agent.py`** | **调度算法 A/B 基准**：压 Claude Agent 型负载（多会话串行回合链 + 工具等待 + 前缀复用），出 TTFT/E2E/prefill/decode/排队/气泡比；支持 `--compare` 两次运行对比 |
| **`PERF.md`** | **性能口径**：两层指标定义、`/v1/perf` 接口、A/B 标准流程（移植算法前后各跑一次的那把尺子） |
| `determinism_probe.py` | 记录"贪婪解码不可逐字节复现"这个事实，供验收标准参考 |
| `demo_multiuser.py` | **多用户接入**演示：N 个用户各带身份从终端提问，超过会话数的排队 |
| `fake_backend.py` | 假后端，本地无板卡时跑网关回归（Windows 上用它，`pass_fds` 不可用） |
| `rknn_smi_temp.py` | **卡温采集**（可选）：以 root 每 5 秒跑一次 `rknn-smi info -t temp`，原子写 `/run/rknn-temp.json`；网关只读这个文件（见「卡温采集」一节） |
| `rknn-temp.service` | 上面那个采集脚本的 systemd 单元（`@SERVE_DIR@` 是安装时替换的占位符） |

## 起服务

```sh
# 1) 构建（在 x86 构建机上，交叉编译）
bash build_serve.sh                       # 产物默认落在仓库根 rknn_multicard_demo.serve

# 2) 把 rkllm_gateway.py / start_gateway.sh / restart_gateway.sh / rknn_multicard_demo.serve
#    放到板卡的同一个目录（下称 $GATEWAY_DIR）

# 3) 启动（模型加载 ~240s）
cd "$GATEWAY_DIR"
INSTALL_DIR=<安装目录>/rknn_multicard_demo \
MODEL_DIR=<模型目录> \
./start_gateway.sh
```

环境变量（都有默认值，见脚本头部注释）：`INSTALL_DIR`（含后端二进制与 `lib/`）、
`MODEL_DIR`、`GATEWAY_DIR`、`B`（后端二进制名）、`NSESSION`（默认 4）、`PORT`（默认 18280）、
`NP`（每轮 max_new_tokens 默认值）、`HOST`（默认 `0.0.0.0`）、`LOG`、
`IDLE_TTL`（默认 300，一段对话静默这么久就把它占的会话收回给排队者；**0=不回收，是这一套的总开关**）、
`CONTEND_IDLE`（默认 15，**已经有人非等不可**时用的那一级阈值，见下；0=关掉这一级）、
`QUEUE_TIMEOUT`（默认 600，取不到会话时最多排队等这么久，超了返回 503；要大于 `IDLE_TTL`）、
`CTX`（上下文长度，默认 4096）、`FORCE_SESSIONS`（默认 0，设 1 跳过下面那道容量闸）。

**`CTX` 是一个"请求"，不是开关**：它只能在模型编进去的那组 KV 候选里挑，权威答案是后端日志里的
`chosen kvcache_buffer_lens`。两种夹法都见过——老 4096 导出请求 8192 只拿到 4096（四个 stage
仍打 `max_ctx_len=4096`）；**32768 导出请求 8192 仍然拿到 32768**（一份导出只有一档 KV，
`n_lens=1`）⇒ **拿大导出想靠 `--ctx-size` 省内存是省不掉的**。核板上是哪一份最快的办法是看
rope 外置张量的大小：每个位置 256 字节 + 432 字节头 ⇒ 8192 → 2.0 MiB、32768 → 8.0 MiB
（形状 `[1, 4, 1, N, 16]`）。

**`NSESSION` 的上限由内存账决定，不是一个固定数**：KV 在每个会话**建出来时按满上下文**一次性
预分配，所以账是 `上下文 × 路数`。实测：4096/8192 导出 **5 路**封顶（第 6 个在 stage0 起不来），
**32768 导出只够 2 路**（每路 406.7 MB/卡，3 路差约 38 MB/卡）。会话**只在启动时**创建，
运行时不新建，所以"同时能服务多少人"= `NSESSION`，多出来的人在队列里等。

⚠️ **超开不是报错，是把四张卡压死**：`rknn3_session_init` 卡在 `wait_event(0x100000): timed out`，
那个端点从此不应答，之后每次加载都是 `ERROR_PIPE`，`systemctl restart rknn3` 也救不回来
（四个 `rknn3_transfer_proxy` 一起没了）⇒ **只能重启板卡**。所以 `start_gateway.sh` 会从 rope
张量反推上下文、算出上限并与 `NSESSION` 比，超了直接拒绝启动；要硬上得显式 `FORCE_SESSIONS=1`。

## 卡温采集（可选；不装也不影响任何别的功能）

**RK1828 是 PCIe 设备**，主机侧 `/sys/class/hwmon`、`thermal_zone` 里**没有**它的温度——
那几个读数全是 RK3588 自己的（`hwmon0~6` 就是 `thermal_zone0~6` 的别名）。四张卡的结温只有
`rknn-smi info -t temp` 拿得到（走 PCIe 直接问卡，`Chip Temperature (C)` 那一行；同一次输出里
`Device Temperature (C)` 在这块板上恒为 `NA`），而它**要 root**：`/dev/pcie-rkep-*` 是
`crw------- root root`，非 root 跑**不是报错、是静默**（rc=255 且一个字都不输出）。

网关**不提权**——它是局域网上**无鉴权**的常驻服务（见「已知限制」），给它一条免密 sudo 等于
把"能拉起 root 进程"变成它的能力。所以温度由板上一个**独立小服务**采，网关只读它写出的文件：

```sh
cd "$GATEWAY_DIR"
python3 rknn_smi_temp.py --selftest                  # 14 PASS / 0 FAIL（不要板卡、不要 root）
sudo ./rknn_smi_temp.py --once && cat /run/rknn-temp.json   # 排障：采一次看有没有数

sed "s|@SERVE_DIR@|$PWD|" rknn-temp.service | sudo tee /etc/systemd/system/rknn-temp.service
sudo systemctl daemon-reload && sudo systemctl enable --now rknn-temp
systemctl status rknn-temp --no-pager                # 在跑
cat /run/rknn-temp.json                              # 四张卡都有 c，且 ts 每 5 秒往前
```

- 输出：`{"ts": <epoch 秒>, "devices": [{"id": 0, "c": 29.0}, …]}`。`c` 为 `null` = 这一次没读到
  （**不是 0**；页面上画 `—`）。写文件走 `mkstemp` + `chmod 0644` + `os.replace`。
- **采集失败时脚本不写文件**：`ts` 不动、页面上的"多少秒前"继续涨——"上次成功是 30 秒前"
  是看得出来的。照写一份空值就成了"服务在跑、只是温度没了"，那是两件事。
- 一次 `-t temp` ≈ **135 ms**，5 秒一轮 ≈ **单核 2.7%**；对推理的影响实测在噪声内。
- ⚠️ **别再手工跑 `rknn-smi info -w`**：同一时刻只能有一个 `rknn-smi`，那个守护式的进程会让
  本服务每一轮都 `Failed to initialize rknnsmi`，而现象只是"页面上的温度变成 `—`"。
- ⚠️ `rknn-smi` 在这块板上**成功时也返回 255**，所以本脚本**不拿退出码当判据**（判据是
  "有没有吐出东西"）；要自己写解析的话别踩这个。
- 不想要了就 `sudo systemctl disable --now rknn-temp`：页面上的卡温画 `—`，**其余功能照旧**。

网关侧对应的是 `--temp-file`（默认 `/run/rknn-temp.json`），启动横幅里会写明这个文件在不在。

## 接口

- `GET /health` → `{"status":"ok","model":...,"sessions":N,"sessions_busy":b,"sessions_bound":k,"waiting":w,"ctx_size":C,"uptime_s":...}`
  （`ctx_size` 是后端命令行里的 `--ctx-size`，取自后端 argv；取不到时为 `null`。
  **演示页靠 `sessions` + `ctx_size` 决定开几个窗口**——见 `DEMO.md` §4.3）
- `GET /v1/models` → OpenAI 格式的模型列表
- `POST /v1/chat/completions` → 兼容 OpenAI；`stream: true` 走 SSE，`stream: false` 一次性返回
- `GET /v1/pool` → 会话池快照，**多用户场景诊断用**（见下）
- `GET /v1/system` → 资源快照，**演示页那一块资源使用的数据源**（生成中每 2s 拉一次、
  待机 6s、页面切到后台就停）。**网关侧对它做了 1s 的快照缓存**：一秒内不管几个人来问
  都只采一次（读十几个 procfs/sysfs + 一次 STAT 往返，出的都是被观测那台机器的钱；
  顺带让"同一秒里两份 doc 的 `ts` 相同"成为必然，页面靠它跳过重复重画）。形状：

  ```json
  {"ts": 1758...,
   "host": {"cpu": {"n": 8, "pct": 12.5, "per_core": [8.0, 3.1, ...], "loadavg": [0.4,0.3,0.2]},
            "mem": {"total": 168..., "available": 60..., "used": 108..., "used_pct": 64.3},
            "thermal": [{"zone": "thermal_zone0", "label": "soc-thermal", "c": 61.2}, ...],
            "uptime_s": 12345},
   "npu": {"ok": true, "busy_pct": 43.1,
           "cards": [{"name": "stage0", "ctx_len": 32768, "run_calls": 41, "busy_pct": 55.0,
                      "mem_total": 5071..., "mem_free": 480..., "mem_used_pct": 90.6,
                      "mem_age_s": 0.4, "node_num": 8, "node_min_free": 13...}, ...]},
   "backend": {"alive": true, "sessions": 2, "ctx_size": 32768, "stats_age_s": 0.4},
   "gateway": {"busy": 1, "bound": 1, "dead": 0, "waiting": 0, "uptime_s": 900}}
  ```

  ⚠️ **几个数别读错**：`npu.busy_pct`（与每卡的 `busy_pct`）是**算出来的**——累计推理
  耗时 ÷ 墙钟，**SDK 没有 NPU 利用率查询接口**；`host.thermal` **只有 RK3588 的热区**
  （RK1828 是 PCIe 设备，没有 hwmon/thermal_zone，拿不到卡温）；**缺值一律 `null`**
  （页面画 `—`，绝不画 `0%`）：`cpu.pct` / `busy_pct` 第一次采样为 `null`（没有前一个点
  可比），`node_min_free` / `node_num` 为 `null` 表示后端没报节点数，`mem_age_s` 为
  `null` 表示**一次都没采到**（与"采过但很旧"是两回事）。后端挂了或答不上来这一接口
  **不报错**：`backend.alive` 变 `false`、`stats_age_s` 变大（页面据此把数标成"旧的"）。
- ⚠️ **`mem_total` / `mem_free` 是这一卡所有 node 之和**（`rknn3_dev_mem_info` 的
  `Σ node.total` / `Σ node.free`，8 个 node 各 ~634 MB ⇒ 整卡 ~5.06 GB）。
  **不要用同一结构里的 `sys_total` / `sys_free`**——那是"本机侧的小块内存"（实测只有
  19.0 / 18.7 MB，占的是驱动与传输缓冲，与模型放不放得下无关）：拿它当整卡内存，
  页面上会把一张 93% 占用的卡画成「19.0 MB」。判断**放不放得下**看 `node_min_free`
  （各 node 里最紧的那个），它才是真正的约束。
- ⚠️ **`busy_pct` 偶尔会重复上一次的值，不是 bug**：这个百分比 = 两次 STAT **答复**之间
  的忙时差 ÷ 墙钟差。网关等后端的答复有时间上限，板上偶发超过它 ⇒ 这一次拿到的还是
  上一份快照（`mem_age_s` 会如实变大）。**同一份数据没变旧，所以原样重复上一个百分比**
  而不是给 `null`——多人同时看页面时，晚到的那个人就不会白闪一下 `—`。
- `POST /v1/stop` → `{"request_id": N}`：**停掉在跑的那一轮**（网页「暂停回答」）。
  `N` 是那一轮流式响应回在 `X-Request-Id` 头里的 rid。停了**照发 `DONE`（`finish_reason=abort`）**，
  由那一轮自己的流式响应收尾；rid 不认识时静默无事发生。
- `POST /v1/chat/completions` 带 `{"continue": true, "conversation_id": ..., "max_tokens": ...}` →
  **继续上一轮被打断的回答**：后端跳过 prefill、接着上一轮的 KV 往下 decode。身份必须和被打断的那一轮
  一致（`acquire_continue` 据此找会话），否则 400；响应始终流式。
- `POST /v1/conversations/close` → **"这段对话说完了"**：立刻把它占的会话交还给排队者（见下）
- `GET /`、`GET /demo` → 网页演示页（网关把同目录的 `demo_4chat.html` 读出来发出去）。
  **每次请求现读磁盘，换页面不用重启网关**。页面启动时先 `GET /health`，按
  `min(4, sessions)` 摆窗口、按 `ctx_size` 显示上下文；窗口可以增删，但**不许超过
  `/health` 报的 `sessions`**（超了那几路只会排队；在 32K 那种大导出上硬凑是会把板卡压死的）。

### 多用户：会话是按「身份」分的，不是按内容

网关默认按 `sha1(system + 首条 user 内容)` 认对话。多人接入时这**不够用**：两个人问同一句话
会撞成同一段对话、被钉在同一个会话上串行——答案全对，但吞吐掉一大截，从输出上看不出来。
所以每个用户必须把自己的身份显式给出来，三选一（优先级从高到低）：

| 位置 | 例子 | 说明 |
|---|---|---|
| 请求体 `conversation_id` | `"conversation_id": "alice"` | 最直观 |
| 请求头 `X-Conversation-Id` | `-H 'X-Conversation-Id: alice'` | 不改 body 就能加，终端用户推荐 |
| 请求体 `user` | `"user": "alice"` | OpenAI 的既有字段，兼容用 |

```sh
# 两个用户各自接入，各占一个会话（不传身份的话这两个请求会被当成同一段对话）
curl -N -s http://<板卡>:18280/v1/chat/completions -H 'X-Conversation-Id: alice' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-27b","messages":[{"role":"user","content":"你好"}],"stream":true}'

curl -N -s http://<板卡>:18280/v1/chat/completions -H 'X-Conversation-Id: bob' \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-27b","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

调度规则（`NSESSION` 个会话，第 N+1 个人来了怎么办）：

- 有会话空闲 → 立刻给，并**记住这个身份以后就归它**；同一身份的后续轮次继续用它（KV 复用）。
- 全都有主且都在跑、或全都被别的身份占着 → **排队**，不抢占。抢占要付的代价是把受害者
  的 KV 丢掉（它下一轮要重新 prefill），而且**抢谁取决于到达顺序**，同一份代码能测出
  3.40×/1.90×/1.00× 三种结果——演示场合最不该有的就是这种不确定性。
- 一段对话静默超过 `IDLE_TTL` → 它占的会话被收回给排队者。被收回的人下次提问前缀对不上，
  会走一次 RESET + 完整 prefill（多约 2–3 秒），**正确性不受影响**（上下文一直在客户端手上）。
- **已经有人在等的时候，上面这条的阈值降到 `CONTEND_IDLE`（默认 15 秒），而且只收最闲的那一段。**
  没有这一级，第 N+1 个人要等到某一段对话静默满 `IDLE_TTL` 才拿得到会话——而那段时间里 NPU
  是**真空着的**，用户看到的就是"明明没人用，我却要等"。它只在"一个空闲会话都找不到"时才发作
  （还有空闲会话时常规路径逐字节不变），也只收一段（收多了会把 KV 复用成片打掉，而等的人是**一个**）；
  `IDLE_TTL=0` 仍是总开关，两级一起关。
- 排队超过 `QUEUE_TIMEOUT` → 返回 **503** + `Retry later or reuse a conversation_id...`，
  不让终端用户无限期干等。
- 这一轮的 prompt 太长、装不进上下文 → 后端在 prefill **之前**就把这一轮拒掉，网关回
  **400** + `context limit reached: ... tokens; start a new conversation or trim the history`。
  这是**客户端要改请求**的错误（历史太长），所以是 400 不是 503；而且**会话不受影响**
  ——它还是好的，下一轮照样能用。客户端的处置就是开一段新对话（换个 `conversation_id`）
  或者裁掉历史。流式下 200 头已经发出去了，改不了状态码，此时同一个错误以 SSE 的
  `error` 事件送达，`type` 是 `invalid_request_error`。

**说完了要说一声**：网关**不知道**一段对话什么时候结束（客户端不发结束消息，浏览器关了也没人
通知），所以默认只能靠 `IDLE_TTL` 超时回收。这对"来问一句就走"的客户端很糟——一个脚本连问
4 个不相干的问题就把 4 个会话占满，把后面所有人挡在队列里整整一个 `IDLE_TTL`。所以有这个端点：

```sh
curl -s http://<板卡>:18280/v1/conversations/close -H 'Content-Type: application/json' \
     -d '{"conversation_id": "alice"}'
# → {"conversation_id": "alice", "key": "id:alice", "closed": true, "session": 0}
```

返回的 `closed` 为 `false` 表示这段对话本来就没占着会话（**幂等**，重复调不报错）。
这一轮还在生成时返回 **409**（不能抽走跑着的会话脚下的 KV）。

`GET /v1/pool` 就是在多用户场景下回答"现在是谁在占着、谁在排队"的——**排队在吞吐上看不出来**，
排队的人不是变慢了，是还没拿到：

```json
{"sessions": 4, "idle_ttl_s": 300.0, "contend_idle_s": 15.0, "queue_timeout_s": 600.0, "reaped_total": 2,
 "slots": [{"session": 0, "state": "busy", "conversation": "id:alice", "key": "id:alice", "idle_s": null},
           {"session": 1, "state": "free", "conversation": null, "key": null, "idle_s": null}],
 "waiting": [{"conversation": "id:dave", "waited_s": 12.4}]}
```

`state` 是 `busy` / `idle`（有主但现在没在跑）/ `free`（无主）/ **`dead`**。
`dead` = 这个会话的驱动线程已经退出（后端在这个会话上回了 `ERR` 帧），网关不会再把活派给它、
也不会把它算进"可用会话"：**看着还有 4 个槽位，实际能用的是 4 减去 dead 的个数**。出现
`dead` 说明后端出过事，去翻网关日志里那个会话的 `ERR` 报文；恢复手段是重启网关（会话只在
启动时创建，运行时不补）。注意 `dead` 的槽位在 `mark_dead` 里已经把归属解绑了，所以它不会
同时带着 `conversation`。
`conversation` 是给人看的标签（长了会截断），`key` 是**原始键**——两者都要有，因为
**匿名对话只能用 `key` 关**（那种对话的身份是网关按 `system + 首问` 算出来的摘要，客户端自己
算不出来）：`curl -d '{"key": "h:faa51b39..."}' .../v1/conversations/close`。
没有这条，"谁占着"看得见却踢不掉。**多人接入还是显式给身份最省事**——匿名对话既认不出也关不掉，
只能等 `IDLE_TTL`。

浏览器跨域是通的：网关对 `OPTIONS` 预检回 204，响应上带 `Access-Control-Allow-Origin: *`
和 `Access-Control-Expose-Headers: X-KV-Reuse`（**没有最后这个头，前端读不到 `X-KV-Reuse`**，
就只能靠猜来判断复用有没有生效）。页面**必须从 `http://<板卡>:18280/demo` 打开**，
双击本地文件走 `file://` 会被浏览器的跨域策略挡掉。

```sh
# 非流式
curl -s http://<板卡>:18280/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen3.5-27b",
  "messages": [{"role":"user","content":"1+1=?"}],
  "max_tokens": 64 }'

# 流式 + 关思考（软开关：网关会给每条 user 贴上 /no_think，并把 <think> 段摘掉）
curl -N -s http://<板卡>:18280/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen3.5-27b",
  "messages": [{"role":"user","content":"1+1=?"}],
  "stream": true,
  "chat_template_kw": {"enable_thinking": false} }'
```

两类扩展：

- **多轮会话粘性**：`messages` 里带上完整历史即可，网关按历史自动把同一段对话钉在同一个会话上，
  跨轮只给后端发**差异部分**（KV 复用）。想强制不复用就换一段全新的对话（首条 user 内容不同）。
  **多人接入时不要靠内容认对话**，用上面的 `X-Conversation-Id` 显式给身份。
- **`X-KV-Reuse` 响应头**：`session=<i>; reset=<0|1>; sent=<n>; base=<n>; full=<n>; wait=<s>`——
  复用有没有生效只看这里。**答案正确但慢 3~4 倍**这类问题，正确性测试抓不到，全靠这个头。
  `wait` 是**拿到会话之前在队列里等的时间**：它非零时，这一轮的总耗时和按总耗时算出来的
  tok/s **都不是解码速度**（SSE 的响应头要等拿到租约才发出去，"变慢"和"还没轮到"在旧版本里
  从响应上分不开，只能去翻网关日志）。

## 已知限制（完整清单见方案文档 §9.7）

- **没有鉴权**。`HOST=0.0.0.0` 时局域网内谁能连上谁就能用满全部算力；Agent 跑在板卡本机时
  建议 `HOST=127.0.0.1`。要对外提供服务请自行加反向代理鉴权。
  **默认端口是 18280**（2026-09-20 从 8080 改的）：换端口**不是**安全控制（同一个局域网里
  扫一遍就找到了），它只是把这块板卡从"默认端口那一份名单"上挪开；顺手做掉的是上面那条
  CORS。**改端口不是单向的**——同事的 SSH 隧道、cc-switch 的 Base URL、浏览器书签都要跟着改。
  **排队策略也架不住这一点**：网关只能按请求上带的身份分会话，没有任何办法核实身份是真的。
  一个客户端只要用 5 个不同的 `X-Conversation-Id` 各发一次，就能占满全部会话，
  让后来的人（包括真人）在队列里等到 `QUEUE_TIMEOUT`。**"超过 5 个排队"只在参与者都守规矩时成立。**
- **排队的公平性是"先到先得"，但不保证严格顺序**：醒来后谁先抢到锁谁先拿，同一批排队的
  用户完成顺序可能与到达顺序不同（实测 5 人 2 会话时是 u3/u5/u4）。不承诺 FIFO 严格性。
- **`IDLE_TTL=0` = 永不回收**（诊断/压测用得着，见测试一节）。**它同时也是 `CONTEND_IDLE` 的
  总开关**——设 0 就是两级回收一起关。这时唯一能释放会话的手段
  就是 `POST /v1/conversations/close`；用匿名对话的客户端**没有**这个手段，会话会被它占到
  网关重启。默认值是 300，不要为了"省事"把它设成 0 跑多人场景。
- **匿名对话占住的会话认得出、踢得掉，但不是常规操作**：得从 `/v1/pool` 读 `key` 再 close。
  按 `conversation_id` 关不掉匿名对话——它没有 id。这是"多人接入应该显式给身份"最硬的理由。
- **CORS 默认一个头都不发**（2026-09-20 改；以前是写死的 `Access-Control-Allow-Origin: *`）。
  `*` 对这套服务是纯风险：API 本来就不要凭据，而 ACAO 是浏览器唯一的门禁 ⇒ 局域网里
  **任何人打开的任意网页**都能背地里驱动这块板卡（读 `/v1/system`、发起生成、拿 `/v1/pool`
  的 key 把别人的对话 close 掉）。演示页是**同源**的（页面由网关自己从 `GET /demo` 发出），
  一个 CORS 头都用不上：同源请求不看 ACAO，`X-KV-Reuse` 同源下也默认可读。
  真要让别的域名来用就显式放行**一个**来源（精确匹配，不做子串）：
  `CORS_ORIGIN=https://那个域名 ./start_gateway.sh`；网关重启横幅里会写明当前放行了谁。
- **演示页开着时占着 4 个会话**（`demo_4chat.html`，身份 `web-1` … `web-4`）。这是有意的：
  同一页里再点一次「②」就是这四段对话的第二轮，KV 复用演示靠它们活着。点「清空」或
  **关掉标签页**会交还（后者走 `sendBeacon`——`beforeunload` 里 `fetch` 会被浏览器取消）。
  所以先演示网页版再演示多用户接入时，中间要关掉页面，否则后来的人全在排队。
  忘了关也能救：`/v1/pool` 里读 `id:web-N`，`POST /v1/conversations/close` 逐个收回来。
- **function calling 已实现，但有下面这几条边界**（格式来自模型自己的 `tokenizer.chat_template`，
  逐字节对拍见 `check_template.py` + `tool_golden.json`）：
  - **`tool_choice: "required"` 不强制调用**。`"none"` 会照做（不注入工具说明），指定单个函数时
    只注入那一个；但**没有任何机制逼模型一定调用**——它仍然可以选择直接回答。要"必须调用"得
    由 Agent 侧自己判（`finish_reason != "tool_calls"` 时重试）。
  - **认不出来的工具调用会作为正文返回**（不猜、不吞）。模型写了个不合格式的块时，客户端拿到
    的是带 `<tool_call>` 标签的正文而不是一个 `tool_calls` 数组——比编一个参数错误的调用安全。
  - **工具集变了会让 KV 前缀失效**。工具说明注入在 system 消息里，所以同一段对话中途增删工具
    等于换了 prompt 前缀，那一轮会全量重算。要求 Agent 在一段对话里保持工具集稳定。
  - **模型回退到 Hermes JSON 形态时，那一轮之后会失去粘性**。渲染侧一律用 XML，所以按 JSON
    解析出来的调用渲回去必然和模型生成的字节不同（`toolcalls.selftest` 里有一条断言钉着这个
    已知限制）。只影响模型自己回退的那些轮。
- **客户端中途断开，这一轮仍会跑完**。后端不能中途叫停一次已经开始 prefill+decode 的推理，
  网关的做法是丢掉往那条连接写的通道、继续把这一轮的 token 读完再释放会话——**代价是这一次
  的连接断开不能换来算力释放**（4 个会话被断开的请求占一秒，这一秒里不会有人补进来）。
  这是刻意选的：反过来做（断开就撤会话）会把跑着的会话脚下抽走，KV 与网关记账立刻不一致，
  下一轮就开始答不对题——那是静默错答，比浪费一点算力糟得多。
- `finish_reason` 是**反推值**（`decode_tok >= max_new_tokens` → `length`，否则 `stop`）。
- 开思考（默认）时推理过程**原样透传**，不单独成字段（协议里没有 `reasoning_content`）。
- 没有 `--no-sticky` 回退开关。
- 同一 prompt 重复请求**不保证逐字节相同**（贪婪解码在本栈上不可复现），验收标准必须是语义级的。

## 测试

```sh
# 板上（网关已起）
./run_all_board_tests.sh http://127.0.0.1:18280      # 结果落 board_tests.log，末尾有 FAIL 汇总
# 注：这套是纯 python3 的，可以直接在能连到板卡的机器上对着 http://<板卡IP>:18280 跑

# 网页演示页（需要 node，不在上面那套里；**任意目录可跑**，按 __dirname/../gateway 定位演示页）
node page_check.js http://<板卡IP>:18280             # 用桩 DOM 跑页面里的真实 JS，打到真板卡
# 它除了原先那几条，还会验：每一路带出去的身份是**它自己的** web-<n>、池子里每个会话分别
# 记着自己的名字、关页面与点「清空」之后会话真的还了回去（后面这一条肉眼看不出来）、
# 面板头那格性能小字写出来了、新一轮开始时那格是空的（挂着上一轮的数比空着更坏）
#
# 2026-09-20 起还验**资源条**：每核小柱的高度要跟着占用率走、**七个热区全画**（不是取前 3 个 + "+4"）
# 且**顺序固定、不按温度排**（每 2 秒重画一次，按温度排会来回跳）、缩写要能互相分辨（big0/big1）、
# 分隔符 · 必须是**真文本**（挂在 CSS ::after 上就复制不出来、读屏也不念）、
# 缺值必须画 —（画成 0% 就是在说"板卡闲着"）、「还没采到」与「超过 6s 没更新」要分开说、
# 后端答不上来时这条不能消失（要标成旧的）、页面切到后台要停止轮询。
#
# 同日起还验**卡温**：四张卡各自一行温度、共用同一个"多少秒前"、**缺值画 — 绝不画 0°**
# （画 0° 是在说这块卡凉快），卡温那对阈值（75/85）与陈旧阈值（20s）都**故意**和热区
# 那套（65/80、6s）不一样——夹具里 70° 必须是无色的，就是为了抓"顺手复用热区阈值"。
#
# 2026-09-20 起还验资源那一块的**位置与开销**：它在对话区（#grid）之后**页脚**那一块、
# 刷新档位待机 6s/生成中 2s、同一份 ts 不许重画（DOM 重写=整块重排一次）、ts 变了必须重画
# （不画就是冻在打开那一帧）、应答里没有 ts 时**也不许冻住**、新一轮开始要立刻补一拍。
#
# 2026-09-20 起还验**窗口的增删与上限**：初始窗口数必须等于 min(4, /health 的 sessions)、
# 页面上限必须等于 /health 的 sessions（自己编一个数就 FAIL）、到上限点「＋」不许多出窗口、
# 新窗口不许复用旧身份（复用=网关认成旧对话，现场看着是"删了没删掉"）、删窗口要当场把
# 会话还回去（先让它真占住一个再删，不然这条检查是空跑也算过）。
#
# 开头还有一组**纯算术** fixture（不联网）：把用户那次真实运行的数喂给页面里的
# summarize()，钉死「各路速率和」与「墙钟摊薄」两个数都要算对、伸缩比只能用前者、
# 被摊薄/排过队时必须当场说破、末句里的路数必须是**当时那几路**（不能写死"四路"）。
# 这几条跑不过会直接 exit，后面的真机部分根本不跑。
# 并发那一轮走的是**页面里 runAll()**（用户点②的那条路），不是检查自己重发一遍——
# 否则汇总行那几句压根不执行，改坏了也全绿。
#
# 它对着**本地桩网关**也能整跑（上面那条 `--port 8099` 的命令起好后）：
#   node page_check.js http://127.0.0.1:8099
# ⚠️ 起桩网关时**窗口数不同的配置各跑一遍**——`sessions=4 @8192`、`sessions=6 @8192`、
# **`sessions=2 @32768`**：窗口数是 4 还是 2 走的是不同分支（能不能「＋」加窗口都不一样），
# 只跑一种会有一整块代码从没被走到。桩网关要把 `--ctx-size` 透给后端（页面靠它显示上下文）。
# 改完页面先在本地过一遍，省一次板卡往返。桩验不到的是真后端的长度与内容行为
# （截断、吐不吐 <think>）以及**排版**（桩 DOM 没有 CSS）——那两样还得上板/用浏览器看。
# ⚠️ 板卡是台**在用的**机器：有人开着 /demo 页面时，池子里本来就躺着 `id:web-*` 的租约。
# 检查开工前会给 `/v1/pool` 拍一张快照，只判**这一轮自己开的**那些身份（别人的会打一句），
# 否则"删窗口没把会话还回去"会指着别人的会话报 FAIL，还没跑到后面几节就 exit(1)。
# 顺带一提：两边页面都从 `web-1` 起数，同一个身份会撞在一起——网关把它当成**同一段对话**，
# 对方的历史顶在前面，前缀对不上就静默整段重算（**答案照样对**，只是那一路变慢）。

# 多用户接入演示（终端，纯标准库；不用 Web 前后端，直接接 API）
python3 demo_multiuser.py http://<板卡IP>:18280 6 96 --stagger 1.5
python3 demo_multiuser.py http://<板卡IP>:18280 6 96 --close      # 问完主动交还，排队的人立刻补上
python3 demo_multiuser.py http://<板卡IP>:18280 6 96 --no-id      # 对照：不带身份会退化成串行

# 本地（无板卡，用假后端）。`--frames-stdout` 是**网关**的开关：帧走子进程的 stdout，
# 绕开 pass_fds（Windows 上直接不支持，实测 AssertionError）。Linux 上不加也能跑。
python3 rkllm_gateway.py --frames-stdout --selftest -- python3 fake_backend.py   # 协议自检
python3 rkllm_gateway.py --frames-stdout -- python3 fake_backend.py &            # 起服务给下面用
python3 serve_http_test.py http://127.0.0.1:18280 quick
python3 check_template.py
```

`fake_backend.py` 的 `--stat-cards`（默认 4）和 `--ctx-len`（默认 8192）**只影响资源条**
（`/v1/system` 里那几张卡）：它按 `ctx_len × 12400` 字节/路 编一套 KV 账，好让本地的
`sessions=2 @32768` 也能画出"快撑爆了"的样子。本地没有 `/proc`，所以 RK3588 那半边
（CPU/内存/温度）在 Windows 上是空的——**这部分只能在板卡上验**。

**`serve_http_test.py` 的每个请求都带 `conversation_id`**（v1.9 起）：网关现在只排队不抢占，
"每问一个新问题就开一段新对话"的写法在会话数用完（默认 4）之后会一路排队到 `IDLE_TTL`，
套件自己就把自己堵死了。**这也正是多人接入该有的写法：身份要显式给，离开要显式说。**

本地桩后端跑整套 HTTP 回归时**要带 `--think-prefix`**，否则"开思考时原样透传"那一项会误报
FAIL——桩默认不出 `<think>`，而真板卡默认出。**建议把 `--idle-ttl 0`（关掉回收）也加上**：
套件能过就说明它真的在靠自己 `close`、而不是碰巧被 TTL 救了：

```sh
python3 rkllm_gateway.py --port 8099 --sessions 4 --idle-ttl 0 --queue-timeout 25 --verbose \
    --frames-stdout -- python3 fake_backend.py --sessions 4 --delay 0.05 --think-prefix
python3 serve_http_test.py http://127.0.0.1:8099        # 全绿（连跑两遍也全绿）
python3 demo_multiuser.py http://127.0.0.1:8099 5 40 --stagger 0.4 --plain --close
```

工具调用（function calling）那一路要桩**主动吐一个调用**才测得到，所以桩要多带 `--tool-call`
（它在 prompt 里还没有 `<tool_response>` 时回一个固定的 XML 调用，拿到工具结果之后就正常回答，
于是整条回路——注入工具说明 → 生成 → 摘出调用 → OpenAI 形态响应 → 回灌结果 → 再问一轮——在
没有板卡的情况下就走通了）：

```sh
python3 rkllm_gateway.py --port 8101 --sessions 2 --frames-stdout \
    -- python3 fake_backend.py --sessions 2 --tool-call --tool-call-repeats --think-prefix
python3 serve_http_test.py http://127.0.0.1:8101 tools
python3 toolcall_check.py http://127.0.0.1:8101        # 桩上只有 NOT、"挑对工具"的观察项
```

`toolcall_check.py` 问的是**另一件事**，桩替代不了：`serve_http_test.py tools` 验的是**我们的
实现**（注入/摘出/回灌/KV），这个脚本验的是**模型**——收到我们渲染的工具目录后会不会调用、
调用的形状我们认不认得、以及模型自己那次调用被客户端**原样回显**回来后前缀还粘不粘得住。
三条判据分开：正文里残留调用标记（`<tool_call>` 等）是 **FAIL**——那是"模型说了、我们没听懂"；
正文是普通文字是模型这轮没调用，单次打 INFO、三次都不调则 FAIL；"选中了该用的那个工具"属于
模型质量，单列 `[NOTE]`，不算失败（桩固定回一个 `get_weather`，拿它当判据会永远红）。
最后那条回显往返只有真模型能回答：模型刚吐出来时记账用参数**原文**，客户端回显走 OpenAI 形态、
原文没了只能按类型重渲，两者逐字节相等当且仅当原文恰好是规范形（`"北京"`、`1.50`、`true`
都不是）。**2026-09-17 实测这一代模型吐的是规范形**，所以回显往返粘得住（热 32 tok / 冷 630）。

**`tools` 模式里有两条性质不同的检查，别把后一条当前一条用**：「模型会不会真调用」取决于模型
意愿（桩上必然发生、板上不一定，没发生就打 `[INFO]` 而不是 `FAIL`）；「回灌 tool 结果再问一轮」
是我们手写历史、不依赖模型意愿的**确定路径**，板上也照跑。里面那条 KV 复用判据比的是**和冷会话
的对照**，不是 `cached_tokens > 0`：后者实测空转过——桩上把记账文本多拼一个空格（前缀必断），
cached 从 567/584 掉到 44/443，`> 0` 依然通过。

**桩要多带 `--tool-call-repeats` 才覆盖得住真模型的行为**。默认的 `--tool-call` 是"调一次、
拿到 tool 结果就正常回答"，而真模型**拿到结果还会再调**（2026-09-17 板上实测：回灌工具结果后它又吐了一次工具调用，那一轮的正文只有一段空的 `<think>`）。这个差别不是细节：`serve_http_test.py` 里那条 KV 判据
下一轮的助手消息，原先只回显**正文**、把 `tool_calls` 丢了；桩不重复调用时那一轮正文恰好非空，
错法完全看不出来，换成真模型立刻红（板卡 **热会话重算 469 tok / 复用 0**，与冷会话一模一样）。
网关的前缀判据是 `prompt.startswith(known[session])`（`rkllm_gateway.py` 的 `_make_lease`），
**全匹配、不做最长公共前缀的部分复用**，所以助手轮少一个字段的代价是整段 prefill 重算。

想单独看"排队 + TTL 回收"这条路，另起一个网关并给一个短的 TTL：

```sh
# CONTEND_IDLE=0：关掉"有人在等就收最闲的"那一级，才看得到纯 TTL 回收（默认 15 秒下，
# 排队的人在你挑的 --idle-ttl 之前就被补上了，这条路上的等待是看不长的）
python3 rkllm_gateway.py --port 8098 --sessions 2 --idle-ttl 3 --contend-idle 0 \
    --queue-timeout 60 --verbose \
    --frames-stdout -- python3 fake_backend.py --sessions 2 --delay 0.05 --think-prefix
python3 demo_multiuser.py http://127.0.0.1:8098 5 20 --stagger 0.3 --plain   # 5 人 2 会话，排队可见
```

`check_template.py` 守的是一条**必须守住的不变量**：服务模式下 prompt 由网关渲染、后端逐字节透传，
所以两边渲染必须**逐字节一致**，否则同一段对话在 `--interactive` 与服务两条前端上的行为会悄悄分叉。

它还管**工具调用**那一半。`main.cc` 里的 `QWEN35_CHAT_TEMPLATE` 没有工具支持，所以权威不是它，
而是模型自己的 `tokenizer.chat_template`；板卡上没有 jinja2（实测 Python 3.12 无此包）渲不了，
于是参考字节在本机用真 Jinja2 渲好后冻进 `tool_golden.json`（生成器 `rt_work/gen_golden.py`，
不进 git）。**这个套件是被"改坏它试试"验过的**：把调用前的分隔、`tojson` 的排序/转义、
`<tool_response>` 的前导换行、连续 tool 消息之间的连接符等处逐个改坏，它每次都红。
