#!/bin/bash
# 重启网关（含后端子进程）。模型加载 ~240s，之后才能收请求。
#
# 为什么单独写成脚本，而不是在命令行里串一串：`pkill -f rkllm_gateway.py` 会**匹配到
# 执行它的那条命令本身**——远端 shell 的 cmdline 里就含这个字符串，于是 pkill 把
# 自己所在的 shell 一起杀了，后面的 rm / 重启全都没执行。现场表现极具误导性：
# 进程没了、日志还是上一次的、看起来像"网关自己崩了"。用 [.] 让模式匹配不到自己
# （正则 [.] 只匹配字面点，而命令行里的文本 'rkllm_gateway[.]py' 不匹配该正则）。
#
# 杀后端那条**同一个坑，而且更容易踩**：`pkill -f rknn_multicard_demo` 会命中
# 调用者的 cmdline，而板卡上 `INSTALL_DIR` 必须显式传、它里面就有
# `.../aarch64/rknn_multicard_demo` 这一段字 —— 于是第 16 行把 shell 自己杀了，
# rm 和重启全没执行，症状同上。所以这里也加括号，并且锚到**带 `.serve` 的完整二进制名**
# （`B` 的默认值）：后端 cmdline 是 `./rknn_multicard_demo.serve ...`，含 `.serve`；
# 目录名不含。**残留风险**：把 `B=./rknn_multicard_demo.serve` 写在命令行上就又会自匹配，
# 那种情况下改成从外面 `unset B` 或单独一条命令传。
#
# 顺带一提：网关退出时**会**带走后端子进程（2026-09-17 实测：kill 掉网关，两个都没了）。
# 这一条因此是"网关非正常死亡（SIGKILL / 崩溃）留下的孤儿后端"的保险——那种情况下
# 四张卡还被占着，不杀干净就起不来新的。别因为它"通常没用"就删掉。
#
# 环境变量：GATEWAY_DIR（默认 = 脚本自己所在目录）、LOG。其余转发给 start_gateway.sh。
# 用法（板上）：INSTALL_DIR=... MODEL_DIR=... ./restart_gateway.sh
set -u
GATEWAY_DIR=${GATEWAY_DIR:-$(cd "$(dirname "$0")" && pwd)}
LOG=${LOG:-$GATEWAY_DIR/gateway.log}
pkill -f 'rkllm_gateway[.]py' && sleep 2
pkill -f 'rknn_multicard_demo[.]serve' && sleep 3
rm -f "$LOG" "$GATEWAY_DIR/gateway_backend.log"
cd "$GATEWAY_DIR" || exit 1
setsid nohup ./start_gateway.sh < /dev/null > "$LOG" 2>&1 &
echo "gateway restarting (model load ~240s); watch $LOG"
