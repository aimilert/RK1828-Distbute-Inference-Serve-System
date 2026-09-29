#!/bin/bash
# 板卡上一次性跑完全部验收测试，结果落盘到 board_tests.log。
#
# 为什么要有这么个脚本：这些测试要么很慢（一次模型加载 ~240s，一轮并发几分钟），要么
# 必须**按顺序**跑（http_scaling 会把 4 个会话都弄脏，正好是后面粘性测试需要的初态）。
# 手工一条条敲既慢又容易漏测项；一条命令全跑完、失败项集中列在最后。
#
# 前置：网关已在跑（../gateway/start_gateway.sh 或 ../gateway/restart_gateway.sh）。
# 用法：./run_all_board_tests.sh [base_url]
#   环境变量：TESTS_DIR（默认 = 脚本自己所在目录）、LOG（默认 $TESTS_DIR/board_tests.log）
set -u
BASE=${1:-http://127.0.0.1:18280}
TESTS_DIR=$(cd "$(dirname "$0")" && pwd)
R=${LOG:-$TESTS_DIR/board_tests.log}
cd "$TESTS_DIR" || exit 1
: > "$R"

run() {
  echo "########## $* ##########" >> "$R"
  local t0 t1
  t0=$(date +%s)
  "$@" >> "$R" 2>&1
  echo "  -> rc=$? 用时 $(( $(date +%s) - t0 ))s" >> "$R"
  echo >> "$R"
}

run python3 check_template.py
run python3 serve_http_test.py "$BASE"
# 同一个脚本的另一个模式：客户端写一个离谱的 max_tokens（1e12）不得打死会话。
# 它要的是**真后端**，因为这条契约（后端拒收超出 int32 的 max_new_tokens）只有真后端
# 和桩各自实现了一份，两边都要能过。放在 full 之后：它会占一个会话几秒钟。
run python3 serve_http_test.py "$BASE" bigmax
# 工具调用。板上"模型这轮会不会真调用"取决于模型意愿（不调用时那几条打 INFO 而不是
# FAIL），但**确定路径**照样全跑：回灌 tool 结果再问一轮、以及带工具的历史能不能粘住
# KV。后者才是重点——工具轮的历史渲染差一个字节，之后每轮就全量重算，不报错、答案也对。
run python3 serve_http_test.py "$BASE" tools
# 上一条验的是**我们的实现**（桩上也能跑），这一条验的是**模型**：真模型收到我们渲染的
# 工具目录后会不会调用、调用的形状我们认不认得、以及模型自己那次调用被客户端原样回显
# 回来后前缀还粘不粘得住。桩替代不了——桩吐什么由我们写死，模型吐什么只有板上知道。
run python3 toolcall_check.py "$BASE"
run python3 sticky_check.py "$BASE"
run python3 nothink_check.py "$BASE"
# 伸缩测量放最后：它会把 4 个会话都写脏
run python3 http_scaling.py "$BASE" 96 1,2,4

# 注意：先把统计结果抓进变量，最后才 >> 追加。直接在这里 `grep "$R"` 又 `>> "$R"`
# 会让 grep 的输入文件同时是输出文件（实测报 "input file is also the output"）。
#
# 只数 `[FAIL]` 是不够的：脚本**崩掉**时一行 FAIL 都不会打（比如网关没起，脚本在
# 第一个请求上抛 ConnectionRefusedError），而崩溃恰恰是最该被当失败的一种结果——
# 只看 FAIL 行的话，汇总会写"（无失败项）"，把一次全灭的跑批报成全绿。
# 所以同时数：非零返回码（run() 每段都记了 rc）和 Python 的 traceback。
NFAIL=$(grep -c '\[FAIL\]' "$R" || true)
FAILLINES=$(grep -n '\[FAIL\]' "$R" || true)
NRC=$(grep -cE -- '-> rc=[1-9][0-9]*' "$R" || true)
RCLINES=$(grep -nE -- '-> rc=[1-9][0-9]*' "$R" || true)
NTRACE=$(grep -c 'Traceback (most recent call last)' "$R" || true)
TRACELINES=$(grep -n 'Traceback (most recent call last)' "$R" || true)
{
  echo "===== 汇总 ====="
  echo "FAIL 行数：$NFAIL"
  if [ -n "$FAILLINES" ]; then echo "$FAILLINES"; else echo "（无 [FAIL] 行）"; fi
  echo "非零返回码的段数：$NRC"
  if [ -n "$RCLINES" ]; then echo "$RCLINES"; fi
  echo "traceback 段数：$NTRACE"
  if [ -n "$TRACELINES" ]; then echo "$TRACELINES"; fi
  if [ "$NFAIL" = 0 ] && [ "$NRC" = 0 ] && [ "$NTRACE" = 0 ]; then
    echo "结论：全绿"
  else
    echo "结论：**有失败**（FAIL 行 / 非零 rc / traceback 任一项非零）"
  fi
} >> "$R"
echo "done; see $R"
# 让调用方（ssh/CI）也能判：全绿才返回 0。以前无论中间怎么炸都返回 0。
if [ "$NFAIL" != 0 ] || [ "$NRC" != 0 ] || [ "$NTRACE" != 0 ]; then
  exit 1
fi
