# Claude Code 接入 RK1828 本地端侧模型 API 指南

> 版本 v1.0 · 板卡：CM3588-Plus（4×RK1828 流水线 + Qwen3.5-27B）· 网关地址 `http://192.168.0.184:18280`

## 0. 一句话结论

Claude Code 说的是 Anthropic Messages 协议，板卡网关说的是 OpenAI Chat Completions
协议，**两者不能直连**。正确做法是用 CC Switch 的「本地路由 + 应用接管」做协议转换：
供应商的「上游格式」选 **OpenAI Chat Completions（需开启路由）**，「请求地址」填板卡根地址，
再打开路由总开关并接管 Claude Code。

## 1. 原理与数据流

```
Claude Code ──Anthropic Messages(/v1/messages)──▶ CC Switch 本地路由 127.0.0.1:15721
                                                      │  协议转换（Anthropic ↔ OpenAI Chat）
                                                      ▼
                                       OpenAI Chat Completions(/v1/chat/completions)
                                                      ▼
                             RK1828 网关 :18280 ──▶ 多会话执行器 ──▶ 4×RK1828 流水线
```

- 开启「应用接管」后，CC Switch 会修改 Claude Code 的配置，把 `ANTHROPIC_BASE_URL`
  **改写成本地路由** `http://127.0.0.1:15721`——这是正常且必须的状态，不是配错。
- 真实上游地址与 Key 只保存在 CC Switch 的供应商配置里，由本地路由在转发时注入。
- 如果 `ANTHROPIC_BASE_URL` 仍然是板卡地址 `http://192.168.0.184:18280`，说明路由/接管
  没有生效：Claude Code 的 `/v1/messages` 请求会直接打到只认 Chat Completions 的板卡，
  结果一定是 404 或协议错误。排查见第 7 节。

## 2. 前置条件

- 板卡网关已启动并加载完模型（模型加载：串行 ~140s / `PARALLEL_LOAD=1` ~62s）：
  `curl http://192.168.0.184:18280/health` 返回 `"status": "ok"`。
- 与板卡同一局域网、能访问 `192.168.0.184:18280`。**只应在可信网络内使用**：该网关无鉴权。
- CC Switch 3.17.0 及以上（含本地路由与 API 格式转换），Claude Code 已安装并至少运行过一次。
- 板卡当前配置：4×RK1828 流水线，模型 Qwen3.5-27B（当前导出为 32768 上下文），
  `NSESSION=2`（最多 2 段对话同时推理，超出进入排队，先到先得、不抢占）。

## 3. CC Switch 供应商配置


| 字段        | 填写                                  | 说明                                           |
| ----------- | ------------------------------------- | ---------------------------------------------- |
| 供应商名称  | RK1828板卡                            | 任意，仅显示用                                 |
| 备注        | 4×RK1828 流水线并行，32K 上下文      | 仅备注；实际上下文以板卡为准                   |
| 官网链接    | https://example.com                   | 可选，不参与连接                               |
| API Key     | board（任意非空字符串）               | 板卡不校验；路由以`Authorization: Bearer` 注入 |
| 请求地址    | http://192.168.0.184:18280            | 完整 URL，**不要以斜杠结尾**                   |
| 上游格式    | OpenAI Chat Completions（需开启路由） | 关键项：告诉路由上游说 Chat 协议               |
| 认证字段    | ANTHROPIC_AUTH_TOKEN（默认）          | 选错（`ANTHROPIC_API_KEY`）常见表现 401/403    |
| Header 覆盖 | `{"X-Provider": "cc-switch"}`         | 仅接管后生效；板卡忽略未知头                   |
| Body 覆盖   | `{"temperature": 0.2}`                | 仅接管后生效；**板卡忽略**（执行器贪婪解码）   |

### 3.1 模型映射

> **⚠️ 模型不要点「获取模型列表」，直接手填 `Qwen3.5-27B` 即可。**
> 每个角色的「实际请求模型」（以及默认兜底模型）都手动写成 `Qwen3.5-27B`：不要用界面上的
> 「获取模型列表 / 一键设置」去拉取再选——这条链路中间还隔着本地路由与协议转换，拉取到的
> 列表不一定可用，手动输入最稳。
> 说明：板卡网关**不校验**请求里的 `model` 字段（`/v1/models` 返回的 id 是小写
> `qwen3.5-27b`，两种大小写都能用，板卡响应统一回 `qwen3.5-27b`）。

| 角色         | 显示名称             | 实际请求模型 | 说明                             |
| ------------ | -------------------- | ------------ | -------------------------------- |
| Sonnet       | Qwen3.5-27B          | Qwen3.5-27B  | 主力档                           |
| Opus         | Qwen3.5-27B          | Qwen3.5-27B  | 映射到同一模型                   |
| Fable        | Qwen3.5-27B          | Qwen3.5-27B  | 映射到同一模型                   |
| Haiku        | Qwen3.5-27B          | Qwen3.5-27B  | **必须填**：后台小任务走这一档   |
| Subagent     | 不显示在 /model 菜单 | Qwen3.5-27B  | 子代理                           |
| 默认兜底模型 | —                    | Qwen3.5-27B  | **必须填**：未命中角色的请求用它 |

- Claude Code 会按角色名发请求（`claude-sonnet-*` 等），板卡只有 `Qwen3.5-27B` 一个模型，
  所以**每个角色都要手填同一个名字**；Haiku 与默认兜底尤其不能留空，否则这些请求会把原始
  Claude 模型名透传给板卡，板卡不认这个名字而报错。
- 「声明支持 1M」只是给 `/model` 菜单的展示声明，不影响真实上下文；板卡上下文由模型导出
  决定（当前 32768）。Claude Code 侧用 `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` 关掉 1M 规划。

## 4. 开启本地路由与接管（必做）

1. CC Switch → 设置 → 路由 → 本地路由：打开「路由总开关」，启动本地服务
   （默认地址 `127.0.0.1:15721`）。
2. 在「路由启用」中打开 **Claude Code**（其余应用可保持关闭）。
3. **重开终端**：环境变量只在终端启动时读取；接管后必须新开一个终端会话，
   正在运行的 Claude Code 也要重启。
4. 验证：

```bash
echo $ANTHROPIC_BASE_URL
# 期望输出：http://127.0.0.1:15721
```

路由模式下切换供应商是热切换，无需重启；开/关接管需要重开终端。

## 5. Claude Code 配置说明

完整配置见附录 A。关键环境变量与作用：


| 变量                                   | 值                         | 作用                                                          |
| -------------------------------------- | -------------------------- | ------------------------------------------------------------- |
| ANTHROPIC_BASE_URL                     | http://192.168.0.184:18280 | 供应商上游地址；**接管后会被改写为 `http://127.0.0.1:15721`** |
| ANTHROPIC_AUTH_TOKEN                   | board                      | 占位符；真实凭据在 CC Switch 里，由路由注入                   |
| ANTHROPIC_MODEL                        | qwen3.5-27b                | Claude Code 默认模型                                          |
| ANTHROPIC_DEFAULT_SONNET_MODEL / _NAME | qwen3.5-27b                | Sonnet 角色映射（其余角色同理）                               |
| ENABLE_TOOL_SEARCH                     | true                       | 开启工具搜索，减少工具定义对上下文的占用                      |
| CLAUDE_CODE_AUTO_COMPACT_WINDOW        | 32768                      | 与板卡 32K 上下文对齐                                         |
| CLAUDE_AUTOCOMPACT_PCT_OVERRIDE        | 75                         | 用到约 75% 时触发自动压缩                                     |
| CLAUDE_CODE_DISABLE_1M_CONTEXT         | 1                          | 关闭 1M 上下文声明                                            |
| CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT       | 1                          | 精简系统提示词，节省上下文                                    |
| effortLevel                            | high                       | 思考强度声明；转换层可传，板卡侧可能忽略                      |
| model                                  | sonnet                     | 会映射到`qwen3.5-27b`                                         |

## 6. 验证

### 6.1 直连板卡（先排除板卡自身问题）

```bash
$ curl -s http://192.168.0.184:18280/health
{"status": "ok", "model": "qwen3.5-27b", "sessions": 2, "ctx_size": 4096, ...}

$ curl -s http://192.168.0.184:18280/v1/models
{"object": "list", "data": [{"id": "qwen3.5-27b", "object": "model", "owned_by": "local"}]}

$ curl -N -s http://192.168.0.184:18280/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.5-27b","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 6.2 Claude Code 内验证

- 新开终端运行 `claude`，用 `/status` 确认端点与模型；
- 发一句「你好」，应流式返回；
- 板卡侧观察：
  `wget -qO- http://127.0.0.1:18280/v1/pool`（会看到 `h:...` 匿名对话占着会话），
  `tail -f ~/rk1828-multisession/gateway/gateway.log`。

### 6.3 性能自测（可选）

- 网页演示：`http://192.168.0.184:18280/demo`
- 终端多路：`python3 demo_4session.py http://192.168.0.184:18280 2 96`

## 7. 故障排查


| 现象                        | 可能原因                                        | 处理                                                                                               |
| --------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| 404 /`not_found` / 协议错误 | 路由没开或没接管，Anthropic 请求直发板卡        | 开路由总开关 + 打开 Claude Code 接管 + 重开终端；`echo $ANTHROPIC_BASE_URL` 应为 `127.0.0.1:15721` |
| 401 / 403                   | 认证字段选了`ANTHROPIC_API_KEY`                 | 改回`ANTHROPIC_AUTH_TOKEN（默认）`                                                                 |
| 400 模型不存在              | 角色未映射，原始 Claude 模型名透传              | 所有角色（尤其 Haiku）与默认兜底都**手填** `Qwen3.5-27B`（不要点「获取模型列表」）                  |
| 一直转圈 / 响应很慢         | 2 个会话被占满，请求在排队                      | `wget -qO- .../v1/pool` 看占用；等空闲或重启网关                                                   |
| 400 上下文超限              | 历史超出 32K                                    | 让自动压缩生效，或开始新会话                                                                       |
| 切供应商不生效              | 终端没重载环境变量                              | 彻底关闭终端重开                                                                                   |
| 工具调用没执行              | `tool_choice: "required"` 不强制调用            | 由 Agent 侧判定`finish_reason != "tool_calls"` 后重试                                              |
| 板卡温度显示「已停」        | 采集服务停了（如手工跑`rknn-smi info -w` 抢占） | `systemctl status rknn-temp`；别跑 `-w` 守护式命令                                                 |

## 8. 板卡侧运维

```bash
# 启动（模型加载：串行 ~140s / PARALLEL_LOAD=1 ~62s；NSESSION 受内存账约束，超限会被容量闸拒绝）
cd ~/rk1828-multisession
INSTALL_DIR=$PWD/install/rknn_multicard_demo \
MODEL_DIR=/home/pi/xyf_workspace/4rk1828_Test/Qwen3.5-27B \
NSESSION=2 bash gateway/start_gateway.sh

# 重启（含后端子进程）
bash gateway/restart_gateway.sh

# 健康检查与日志（板卡上没有 curl，用 wget 或 python3）
wget -qO- http://127.0.0.1:18280/health
tail -f gateway/gateway.log gateway/gateway_backend.log

# 卡温采集服务（可选）
systemctl status rknn-temp --no-pager
```

- 容量闸：启动时从 rope 张量反推上下文并校验 `NSESSION`，超了直接拒绝启动；
  **不要用 `FORCE_SESSIONS=1` 硬上**——超开会把四张卡压死，只能重启板卡。
- 当前 32768 导出每卡只够 2 路；换成 8192 导出可到 5 路（每路 KV 约 100 MB/卡）。

## 9. 已知限制与注意事项

- **无鉴权**：任何能连上 `192.168.0.184:18280` 的人都能用满板卡算力；不要暴露到公网，
  建议只在可信内网使用。
- **并发 2 路**（当前 32K 导出）：多开 Claude Code、后台子任务同时发请求时会排队；
  排队时间可从响应头 `X-KV-Reuse` 的 `wait` 字段看到。
- **匿名对话**：Claude Code 不传 `conversation_id`，板卡按「system + 首条 user 内容」的摘要
  认对话（池里显示 `h:...`）；静默不会主动回收，只在有人排队时按 `IDLE_TTL` /
  `CONTEND_IDLE` 回收；重启网关会清空全部会话。
- **协议转换有损**：Anthropic 的思考块、提示缓存（`cache_control`）等概念在 Chat 协议里
  没有对应物；板卡的思考过程会原样出现在正文里。
- **工具调用边界**：`tool_choice: "required"` 不强制调用；认不出的调用按正文返回；
  同一段对话中途增删工具会让 KV 前缀失效（变慢，答案仍正确）。
- **采样参数无效**：板卡执行器是贪婪解码，不读 `temperature` / `top_p`；
  Body 覆盖里的 `temperature` 不会生效。
- 同一 prompt 重复请求不保证逐字节一致，验收以语义为准。

## 附录 A：Claude Code 配置全文（settings.json）

```json
{
  "attribution": {
    "commit": "",
    "pr": ""
  },
  "effortLevel": "high",
  "enabledPlugins": {
    "agent-skills@addy-agent-skills": true
  },
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "board",
    "ANTHROPIC_BASE_URL": "http://192.168.0.184:18280",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.5-27b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "qwen3.5-27b",
    "ANTHROPIC_MODEL": "qwen3.5-27b",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "32768",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "75",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
    "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT": "1"
  },
  "extraKnownMarketplaces": {
    "addy-agent-skills": {
      "source": {
        "repo": "addyosmani/agent-skills",
        "source": "github"
      }
    }
  },
  "includeCoAuthoredBy": false,
  "model": "sonnet",
  "permissions": {
    "allow": [
      "Bash(g++ -std=c++14 -fsyntax-only -Iinclude -I../include/ffmpeg src/ResourceMonitor.cpp)"
    ]
  },
  "tui": "fullscreen"
}
```

> 注意：接管生效后 CC Switch 会把 `ANTHROPIC_BASE_URL` 改写成 `http://127.0.0.1:15721`；
> 若你看到的仍是板卡地址，说明路由/接管未生效（见第 4、7 节）。

## 附录 B：板卡接口速查


| 接口                      | 方法 | 用途                              |
| ------------------------- | ---- | --------------------------------- |
| `/health`                 | GET  | 健康检查、会话数、上下文          |
| `/v1/models`              | GET  | 模型列表（`qwen3.5-27b`）         |
| `/v1/chat/completions`    | POST | 对话补全（流式/非流式、工具调用） |
| `/v1/stop`                | POST | 停止在跑的一轮（`request_id`）    |
| `/v1/conversations/close` | POST | 主动交还会话                      |
| `/v1/pool`                | GET  | 会话池快照（谁占着、谁在排队）    |
| `/v1/system`              | GET  | 主机与四卡资源快照                |
| `/`、`/demo`              | GET  | 网页演示页                        |

## 附录 C：CC Switch 官方参考

- 代理服务与 API 格式转换：https://ccswitch.co/zh/docs/proxy-service.html
- 应用路由（接管与本地路由地址）：https://ccswitch.co/zh/docs/proxy-routing.html
- Claude / Codex 路由指南：https://ccswitch.io/zh/tutorials/claude-codex-routing-guide
