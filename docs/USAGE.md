# 板端 OpenAI 兼容服务（多会话）

跑在板卡上的 OpenAI 兼容 HTTP 网关，后端是 `src/main.cc --serve` 的多会话并发执行器。
系统概览、目录结构、构建与**部署运行**见仓库根 [`README.md`](../README.md)。

> 要给人演示：见 [`DEMO.md`](DEMO.md)。变更与踩坑见 [`CHANGELOG.md`](CHANGELOG.md)。

## 1. 文档地图

| 文档 | 面向 | 内容 |
|---|---|---|
| `USAGE.md`（本文） | 使用者 | 接口、会话调度、已知限制、测试——**怎么用** |
| [`DEMO.md`](DEMO.md) | 演示者 | 4 路并发演示的完整流程 |
| [`CHANGELOG.md`](CHANGELOG.md) | 开发者 | 按日期的变更与踩坑 |
| [`PERF.md`](PERF.md) | 评测者 | 指标口径、`/v1/perf`、A/B 标准流程 |
| [`Claude接入RK1828本地模型API指南.md`](Claude接入RK1828本地模型API指南.md) | 集成者 | CC Switch 接入 how-to |

## 2. 组成

| 文件 | 作用 |
|---|---|
| `rkllm_gateway.py` | 网关本体（python3 **标准库**，无第三方依赖）；父进程，自己拉起后端子进程 |
| `start_gateway.sh` / `restart_gateway.sh` | 启动 / 杀掉重启（参数走环境变量） |
| `demo_4chat.html` | 网页演示：4 个对话框并排，由 `GET /demo` 发出，无需额外 web 服务 |
| `demo_4session.py` / `demo_multiuser.py` | 命令行演示：4 路进度条 / N 用户接入与排队 |
| `mlfq_sched.py` | 调度内核（纯逻辑）；`SCHED_POLICY=mlfq` 才启用，默认 `fcfs` |
| `toolcalls.py` | 工具调用（function calling）的渲染与解析，含自检 |
| `tool_golden.json` / `check_template.py` | 工具渲染金标准；prompt 渲染**逐字节**比对 |
| `serve_http_test.py` / `toolcall_check.py` / `sticky_check.py` / `nothink_check.py` / `http_scaling.py` | 接口面 / 真模型工具调用 / 粘性复用 / 关思考 / 并发伸缩 |
| `run_all_board_tests.sh` | 板上跑完全部验收测试，失败项集中列在最后 |
| `bench_agent.py` | 调度 A/B 基准（压 Claude Agent 型负载，出 TTFT/E2E/prefill/decode/排队/气泡比） |
| `fake_backend.py` | 假后端；无板卡时本地跑网关回归 |
| `rknn_smi_temp.py` / `rknn-temp.service` | 卡温采集（可选，见 §5） |
| `determinism_probe.py` | 记录"贪婪解码不可逐字节复现"这一事实 |

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{status, model, sessions, sessions_busy, sessions_bound, waiting, ctx_size, uptime_s}` |
| GET | `/v1/models` | OpenAI 格式模型列表 |
| POST | `/v1/chat/completions` | 兼容 OpenAI；`stream:true` 走 SSE |
| POST | `/v1/stop` | `{"request_id":N}`：停掉在跑的一轮（`N` 取自 `X-Request-Id`）；照发 `DONE(finish_reason=abort)` |
| POST | `/v1/chat/completions`（`{"continue":true,…}`） | 继续上一轮被打断的回答（跳过 prefill）；身份须与被打断那轮一致，否则 400 |
| POST | `/v1/conversations/close` | 立刻交还会话（见 §4） |
| GET | `/v1/pool` | 会话池快照（谁占着、谁在排队） |
| GET | `/v1/system` | 资源快照（演示页数据源；网关侧 1s 快照缓存） |
| GET | `/`、`/demo` | 网页演示页（每次请求现读 `demo_4chat.html`，换页面不用重启） |

- **`X-KV-Reuse` 响应头**：`session=<i>; reset=<0|1>; sent=<n>; base=<n>; full=<n>; wait=<s>`。
  复用有没有生效只看这里；`wait` 是**拿到会话前在队列里等的时间**（非零时这一轮的总耗时不是解码速度）。
- **多轮粘性复用**：带上完整历史即可，网关跨轮只发**差异部分**；换一段新对话（首条 user 不同）即强制不复用。
- **关思考**：`chat_template_kw.enable_thinking=false`（网关给 user 贴 `/no_think` 并摘掉 `<think>` 段）。
- **`/v1/system` 读数注意**：`busy_pct` 是"累计推理耗时 ÷ 墙钟"**算出来的**（SDK 无利用率接口）；
  `host.thermal` 只有 RK3588 热区（RK1828 无 hwmon）；缺值一律 `null`（页面画 `—`）；
  `mem_total/mem_free` 是整卡**各 node 之和**（**不要**用同结构的 `sys_total/sys_free`，那是本机侧小块内存）；
  判断放不放得下看 `node_min_free`。

## 4. 多用户与会话调度

网关默认按 `sha1(system + 首条 user 内容)` 认对话。多人接入**必须显式给身份**（否则两人问同一句会撞成同一段对话、串行），三选一（优先级从高到低）：

| 位置 | 例子 |
|---|---|
| 请求体 `conversation_id` | `"conversation_id":"alice"` |
| 请求头 `X-Conversation-Id` | `-H 'X-Conversation-Id: alice'` |
| 请求体 `user` | `"user":"alice"` |

调度规则（`NSESSION` 个会话，第 N+1 个人来了怎么办）：

- 有会话空闲 → 立刻给，并**记住这个身份**；同一身份的后续轮次继续用它（KV 复用）。
- 全被占 → **排队，不抢占**（抢占会丢受害者 KV、下一轮全量重算，且"抢谁"依赖到达顺序，结果不确定）。
- 一段对话静默超 `IDLE_TTL` → 收回其会话给排队者；被收回者下次前缀对不上 → 走一次 RESET + 完整 prefill（约 +2~3 s，**正确性不受影响**）。
- 已经有人在等时，上述阈值降到 `CONTEND_IDLE`（默认 15 s），且**只收最闲的一段**。
- 排队超 `QUEUE_TIMEOUT` → **503** + `Retry later or reuse a conversation_id...`。
- 本轮 prompt 装不进上下文 → 后端在 prefill **之前**拒掉，网关回 **400** + `context limit reached...`（会话不受影响）；
  流式下 200 头已发出，同一错误以 SSE `error`（`type=invalid_request_error`）送达。

**说完了要说一声**：网关不知道对话何时结束，默认只能靠 `IDLE_TTL` 回收。"问一句就走"的客户端应主动交还：

```sh
curl -s http://<板卡>:18280/v1/conversations/close -H 'Content-Type: application/json' \
     -d '{"conversation_id":"alice"}'
# → {"conversation_id":"alice","key":"id:alice","closed":true,"session":0}
```

- `closed:false` = 本来就没占会话（**幂等**）；这一轮还在生成时返回 **409**。
- **匿名对话只能用 `key` 关**（其身份是摘要，客户端算不出来）：`-d '{"key":"h:faa51b39..."}'`。

`GET /v1/pool` 回答"现在谁占着、谁在排队"（排队在吞吐上看不出来）：

```json
{"sessions":4,"idle_ttl_s":300.0,"contend_idle_s":15.0,"queue_timeout_s":600.0,"reaped_total":2,
 "slots":[{"session":0,"state":"busy","conversation":"id:alice","key":"id:alice","idle_s":null}],
 "waiting":[{"conversation":"id:dave","waited_s":12.4}]}
```

`state` = `busy`（在跑）/ `idle`（有主没在跑）/ `free`（无主）/ **`dead`**（驱动线程已退出，不能再用；
"看着 4 个槽位，实际可用 = 4 − dead 个数"，恢复靠重启网关）。`conversation` 是给人看的标签，`key` 是原始键。

**跨域**：网关对 `OPTIONS` 预检回 204，响应带 `Access-Control-Allow-Origin: *` 与
`Access-Control-Expose-Headers: X-KV-Reuse`。页面**必须从 `http://<板卡>:18280/demo` 打开**（`file://` 会被挡）。

```sh
curl -s http://<板卡>:18280/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.5-27b","messages":[{"role":"user","content":"1+1=?"}],"max_tokens":64}'
```

## 5. 卡温采集（可选；不装不影响其他功能）

RK1828 是 PCIe 设备，主机侧 `hwmon`/`thermal_zone` 里**没有**它的温度（那些是 RK3588 自己的）。
卡温只能由 `rknn-smi info -t temp` 拿到，且**要 root**。网关不提权（它是无鉴权的常驻服务），
所以由一个独立小服务采集、网关只读它写的文件：

```sh
cd "$GATEWAY_DIR"
python3 rknn_smi_temp.py --selftest          # 14 PASS / 0 FAIL（不要板卡、不要 root）
sed "s|@SERVE_DIR@|$PWD|" rknn-temp.service | sudo tee /etc/systemd/system/rknn-temp.service
sudo systemctl daemon-reload && sudo systemctl enable --now rknn-temp
cat /run/rknn-temp.json                       # 四张卡都有 c，ts 每 5 秒往前
```

- 输出 `{"ts":<epoch>,"devices":[{"id":0,"c":29.0},…]}`；`c` 为 `null` = 这次没读到（**不是 0**）。
- 采集失败时**不写文件**（`ts` 不动，页面"多少秒前"继续涨）。一次 `-t temp` ≈ 135 ms，对推理影响在噪声内。
- ⚠️ **别再手工跑 `rknn-smi info -w`**（同时只能有一个 `rknn-smi`，会让采集每轮失败）。
- ⚠️ `rknn-smi` 成功时也返回 255，本脚本**不拿退出码当判据**。
- 网关侧对应 `--temp-file`（默认 `/run/rknn-temp.json`）。不想要了：`sudo systemctl disable --now rknn-temp`。

## 6. 已知限制

- **没有鉴权**。`HOST=0.0.0.0` 时局域网内谁能连上谁就能用满算力；本机 Agent 建议 `HOST=127.0.0.1`。
  默认端口 18280（换端口**不是**安全控制）；**排队策略架不住**——用 5 个不同 `X-Conversation-Id` 就能占满会话，
  "超过 N 人排队"只在参与者守规矩时成立。
- **排队公平性是"先到先得"，不保证严格 FIFO**（唤醒后抢锁顺序）。
- **`IDLE_TTL=0` = 永不回收**，同时也是 `CONTEND_IDLE` 的总开关；此时唯一释放手段是 `close`。
- **匿名对话只能靠 `key` 关**（见 §4），因此多人接入应显式给身份。
- **CORS 默认一个头都不发**（2026-09-20 起）。演示页同源，用不到；要放行外部域名用
  `CORS_ORIGIN=https://<域名> ./start_gateway.sh`（精确匹配，不做子串）。
- **演示页开着时占 4 个会话**（身份 `web-1`…`web-4`，这是有意的）。点「清空」或关标签页会交还
  （后者走 `sendBeacon`）。先演示网页再演示多用户时，中间要关页面；忘了关可从 `/v1/pool` 读 `id:web-N` 逐个 close。
- **function calling 已实现，边界如下**：
  - `tool_choice:"required"` **不强制调用**（`"none"` 生效；指定单函数只注入那一个）——"必须调用"由 Agent 侧判（`finish_reason != "tool_calls"` 重试）。
  - **认不出的工具调用按正文返回**（带 `<tool_call>` 标签），比编一个错的调用安全。
  - **工具集变了会让 KV 前缀失效**（工具说明注入在 system 里）——同一对话里保持工具集稳定。
  - 模型回退到 Hermes JSON 形态的那一轮之后**失去粘性**（渲染侧一律用 XML）。
- **客户端中途断开，这一轮仍跑完**（后端不能中途叫停已开始的推理）——代价是断开不能换来算力释放，
  但换来了 KV 与记账的一致（否则会静默错答）。
- `finish_reason` 是**反推值**（`decode_tok >= max_new_tokens` → `length`，否则 `stop`）。
- 开思考（默认）时推理过程**原样透传**，不单独成字段；**没有** `--no-sticky` 回退开关。
- 同一 prompt 重复请求**不保证逐字节一致**（贪婪解码不可复现），验收标准必须是语义级的。

## 7. 测试

```sh
# 板卡验收（纯 python3，也可在能连板卡的机器上跑）
./run_all_board_tests.sh http://127.0.0.1:18280     # 结果落 board_tests.log，末尾有 FAIL 汇总

# 网页演示页（需 node；按 __dirname 定位演示页，任意目录可跑）
node page_check.js http://<板卡IP>:18280            # 桩 DOM 跑页面真实 JS，打到真板卡

# 多用户接入演示（终端）
python3 demo_multiuser.py http://<板卡IP>:18280 6 96 --stagger 1.5
python3 demo_multiuser.py http://<板卡IP>:18280 6 96 --close      # 问完主动交还
```

本地无板卡时用假后端（`--frames-stdout` 让帧走 stdout，绕开 Windows 不支持的 `pass_fds`）：

```sh
python3 rkllm_gateway.py --frames-stdout --selftest -- python3 fake_backend.py        # 协议自检
python3 rkllm_gateway.py --port 8099 --sessions 4 --idle-ttl 0 --queue-timeout 25 --verbose \
    --frames-stdout -- python3 fake_backend.py --sessions 4 --delay 0.05 --think-prefix
python3 serve_http_test.py http://127.0.0.1:8099    # 全绿（连跑两遍也全绿）
python3 check_template.py                           # prompt 渲染逐字节比对

# 工具调用那一路（桩要主动吐调用）
python3 rkllm_gateway.py --port 8101 --sessions 2 --frames-stdout \
    -- python3 fake_backend.py --sessions 2 --tool-call --tool-call-repeats --think-prefix
python3 serve_http_test.py http://127.0.0.1:8101 tools
python3 toolcall_check.py http://127.0.0.1:8101     # 桩上只有 NOT 与观察项
```

- 桩后端跑整套 HTTP 回归时**要带 `--think-prefix`**（否则"开思考原样透传"会误报）；建议加 `--idle-ttl 0`
  （套件能过才说明它真在靠自己 `close`）。
- `serve_http_test.py` 每个请求都带 `conversation_id`（否则会话用完会自己堵死）——这**也正是多人接入该有的写法**。
- `toolcall_check.py` 验的是**模型**（会不会调、形状认不认得、回显后 KV 粘不粘），桩替代不了；桩固定回一个调用，
  判据分三层（正文残留调用标记 = FAIL；没调用打 INFO；选中该用的工具是 `[NOTE]` 不算失败）。
- `check_template.py`：服务模式下 prompt 由网关渲染、后端逐字节透传，两边渲染必须**逐字节一致**。
  工具那一半以模型自己的 `tokenizer.chat_template` 为准（参考字节冻在 `tool_golden.json`）。
