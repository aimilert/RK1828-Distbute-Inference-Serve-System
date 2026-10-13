# 性能统计与调度算法 A/B 口径

这份文档只讲一件事：**怎么在板卡上量出可信、可对比的性能数字**。它服务于
`algorithm/mlfq/`（服务端 MLFQ 调度算法）向端侧的移植验证——移植前先有基线，
移植后用同一把尺子再量一遍。

## 1. 两层指标，缺一不可

| 层 | 谁来测 | 能测到什么 | 测不到什么 |
|---|---|---|---|
| 客户端（`bench_agent.py`） | 请求方 | TTFT、E2E、makespan、总吞吐、气泡比 | prefill/decode 的拆分、排队时长 |
| 网关（`/v1/perf`） | 服务方 | prefill/decode 速率、TPOT、排队等待、KV 命中、气泡比 | 网络与客户端开销 |

**只看一层会误判**：客户端 TTFT 变长，可能是解码变慢，也可能是排队变长（吞吐没掉）；
网关数字漂亮，客户端也可能因为网络/转换层而变慢。两份都读，差值就是"排队 + 传输"。

## 2. 指标定义（写死在这里，避免各自解读）

| 指标 | 定义 | 来源 |
|---|---|---|
| `ttft_ms`（网关） | REQ 帧写进后端 stdin → 第一段**非空**文本。**不含排队** | DONE/帧流 + 网关单调钟 |
| `e2e_ms`（网关） | REQ 帧写出 → DONE。**不含排队** | 同上 |
| `queue_wait_s` | 会话池排队等待秒数（`X-KV-Reuse` 里的 `wait`） | 会话池记账 |
| `prefill_tok_s` | `prefill_tokens ÷ prefill_ms`（DONE 帧的真实 token 数与耗时） | DONE 帧 |
| `decode_tok_s` | `decode_tokens ÷ decode_ms` | DONE 帧 |
| `tpot_ms` | `decode_ms ÷ decode_tokens`（每个输出 token 的毫秒） | DONE 帧 |
| `cache_hit_ratio` | `Σcached_tokens ÷ Σprompt_tokens`（KV 复用省下的比例） | usage/DONE |
| `bubble_ratio` | `1 − Σ会话忙时 ÷ (N × 窗口跨度)`。忙时用 `e2e_ms` 近似（拿到租约即算忙） | 网关聚合 / 客户端 |
| `makespan` | 从最早一个请求发出到最晚一个请求结束的墙钟 | 客户端 |
| `output_tokens_s` | **总输出 token ÷ makespan**。把排队、prefill、别人的尾巴都摊进分母——"这批活平均多快" | 客户端 |
| `per_lane_sum_tok_s` | **各路速率和**：每一路 `自己的 token ÷ 自己的端到端窗口`，再相加；**不含排队**。这是演示网页上那个数（`伸缩比`的分母口径也用它）——"并发起来时能跑多快" | 客户端 |

> ⚠️ 这两个吞吐**经常差很多**，别混用：例如 4 个会话抢 2 个槽位时，有 2 个会话要排队
> 几十秒，`output_tokens_s` 会被 queue 拉低，而 `per_lane_sum_tok_s` 仍反映并发时的
> 速率。**要和网页/演示的数字对比，用 `per_lane_sum_tok_s`。**

## 3. 网关新增的观测面

```bash
# 聚合窗口（默认最近 10000 条，进程内滚动，重启即清）
curl -s http://127.0.0.1:18280/v1/perf

# 连每条明细一起给（离线画图 / 回归）
curl -s 'http://127.0.0.1:18280/v1/perf?full=1'

# A/B 的起跑线：先清空，再打负载，跑完读一次
curl -s -X POST http://127.0.0.1:18280/v1/perf/reset
```

另外两处也能拿到每轮明细（都是**非标准扩展**，不影响任何 OpenAI 客户端）：

- 流式：最后一个带 `usage` 的 chunk 里多了 `"perf": {...}`；
- 非流式：响应体顶层多了 `"perf": {...}`；
- 日志：每条请求一行 `[perf] {json}`（网关 `--verbose` 时）。

`perf` 对象的字段与第 2 节一一对应，另有 `rid / session / finish / reset /
sent_bytes / base_bytes / full_bytes / ctx_tokens` 便于定位。

## 4. 基准负载：`bench_agent.py`

它压的是**与 Claude Code 同构**的负载：多会话、每会话严格串行回合链、轮间工具等待、
同会话前缀复用、全程流式。这正是上层的调度自由度所在（见 `algorithm/mlfq/scheduler.py`
的语义说明）。

```bash
# 基线（默认 2 会话 = 当前 32768 导出的 NSESSION 上限）
python3 bench_agent.py --base http://127.0.0.1:18280 --label baseline --tools

# 移植算法后，同样一条命令再跑一次
python3 bench_agent.py --base http://127.0.0.1:18280 --label mlfq --tools

# 对比（方向已内置：越小越好的指标自动判"更好/更差"）
python3 bench_agent.py --compare perf_results/bench_baseline_*.json \
                                perf_results/bench_mlfq_*.json
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--sessions` | 2 | 并发会话数（Agent 并行对话数；= 板卡 `NSESSION`） |
| `--rounds` | 3 | 每会话回合数（越长，prefill 成本被摊得越薄） |
| `--prompt-bytes` | 2000 | 每轮**新增**字节数（前缀复用靠只追加历史） |
| `--output-tokens` | 96 | 每轮 `max_tokens` |
| `--tool-wait` | 0.5 | 回合间工具等待秒数（补位窗口） |
| `--stagger` | 0 | 会话错峰启动（模拟 Agent 不同时刻接入） |
| `--tools` | 关 | 注入 3 个工具定义（Agent 真实会带工具） |
| `--think` | 关 | 开思考（对解码速率影响很大，A/B 时必须一致） |
| `--keep-conversations` | 关 | 跑完**不** `close` 会话（默认会 close：不交还的话下一轮要等回收，连跑的 p99 会被污染） |

输出：

- 控制台摘要表；
- `tools/perf_results/bench_<label>_<时间>.json`（全量明细）；
- `tools/perf_results/bench_results.csv`（追加一行，列名与
  `algorithm/mlfq/analyze.py` 的 `METRICS` 对齐，可直接分析）。

## 5. 标准 A/B 流程

1. **固定条件**：同一份模型导出、同一个二进制、同一个 `NSESSION/CTX`、
   同样的 `--rounds/--prompt-bytes/--output-tokens/--think/--tools`。
2. `perf-baseline` 版本跑一轮，记 `--label baseline`。
3. 移植算法（只改调度，不改模型/二进制），重启网关。
4. 用**完全相同**的命令跑 `--label <算法名>`。
5. `--compare` 出结论；再看 `/v1/perf` 的 prefill/decode/排队拆分，解释差异从哪来。
6. 若要写进报告：把 `bench_results.csv` 的两行喂给 `algorithm/mlfq/analyze.py`
   （消融权重/回归那套可直接复用）。

**至少连跑 3 轮取中位数**：这套栈的贪婪解码不保证逐字节复现，单轮差异里有噪声。

## 6. 调度策略 A/B

网关的会话分配策略（`fcfs` / `mlfq` / `sjf` / …）用 `SCHED_POLICY` 在**启动时**选。策略的完整说明见 `gateway/mlfq_sched.py` 与 `CHANGELOG.md`；本节只留与指标口径相关的要点：

- **必须有排队，策略才有作用**：bench 的会话数要大于 `NSESSION`，且用 `--hetero` 制造长短差异
  （同构负载下打分无区分度 → 退化为 FCFS）。
- A/B 起跑线：`POST /v1/perf/reset` → 打负载 → 读 `/v1/perf`；同负载**至少连跑 3 轮取中位数**。
- 除速率外，移植算法还可能要比**功耗**（板卡另有功耗统计器）——把功耗与 `output_tokens_s` 一起看。

## 7. 注意事项

- 每次跑之前确认模型已加载完（`/health` 返回 `ok`）；模型加载：串行 ~140s / `PARALLEL_LOAD=1` ~62s。
- A/B 之间**不要让别的客户端同时打板卡**（Claude Code、演示页都会抢会话）：
  先 `POST /v1/perf/reset`，跑完立刻读，避免把别人的请求算进来。
- `--sessions` 超过板卡 `NSESSION` 时是刻意制造排队；对齐算法收益时不要超过。
- 气泡比在客户端与网关各算一份：两者定义一致（忙时近似），差值是网络/读取开销量级。
- 除了速率，移植算法还可能要对比**功耗**（板卡另有功耗统计器）——把功耗与
  `output_tokens_s` 放在一起看，才能说明"同样的电换来多少吞吐"。
