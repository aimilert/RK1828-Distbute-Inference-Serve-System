#!/usr/bin/env bash
# 在**板卡上原生编译** --serve 版执行器。产出与 `build_serve.sh`（交叉编译）是同一件东西。
#
# 为什么要有它：租的交叉编译机到期之后，板卡成了唯一的构建机。它只需要：
#   · src/main.cc（本仓库的工作区原件；**别在板上改代码**，板上没有 git）
#   · include/ 下的头：Tokenizer.h / float16.h / rknn3_api.h，以及 nlohmann/json.hpp
#   · libtokenizer.a（静态链进去；由 LIBTOKENIZER 指向）
#   · 链接期找得到的 librknn3_api.so（由 INSTALL_DIR/lib 提供），运行时靠 rpath
#     `$ORIGIN/lib` 找
#
# 优化选项抄的是工程的 Release 口径（`-O2 -s`）。**不要在这里自作主张换 -O3 之类**：
# 这套 demo 的吞吐数字（10.97 / 20.22 / 37.13 tok/s 那一组）都是 Release 口径，
# 换了优化级别就不可比了。
#
# 环境变量：
#   LIBTOKENIZER  静态 tokenizer 库路径（必填）
#   INSTALL_DIR   装了 lib/librknn3_api.so 的目录（也是产物的默认安装位置）
#   OUT           产物路径；默认 $INSTALL_DIR/rknn_multicard_demo.serve
#   KEEP_BAK      1 = 装之前把同名旧产物另存成 .bak_<md5>（默认 1）
#
# 用法（板上；默认用本仓库 third_party/ 里的 aarch64 库，无需参数）：
#   bash build_serve_native.sh
# 要换 SDK / 架构：
#   TARGET_ARCH=aarch64 LIBTOKENIZER=.../libtokenizer.a \
#   INSTALL_DIR=.../install/rknn_multicard_demo bash build_serve_native.sh
#
# 与 build_serve.sh 同样的道理，这里也必须 `set -e` 且**对产物做断言**：只打印不判断
# 的话，"编译失败"和"编译成功"的输出长得一模一样，而装上去的是上一轮的旧二进制。
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
# 默认用本仓库 third_party/ 下的 aarch64 预编译库；要换架构或换 SDK 用 TARGET_ARCH /
# LIBTOKENIZER / INSTALL_DIR 覆盖。
ARCH=${TARGET_ARCH:-aarch64}
LIBTOKENIZER=${LIBTOKENIZER:-$ROOT/third_party/tokenizer/lib/Linux/$ARCH/libtokenizer.a}
LIBDIR=${LIBDIR:-$ROOT/third_party/rknpu3/Linux/$ARCH}
INSTALL_DIR=${INSTALL_DIR:-$ROOT/install/rknn_multicard_demo}
OUT=${OUT:-$INSTALL_DIR/rknn_multicard_demo.serve}
KEEP_BAK=${KEEP_BAK:-1}
SRC=$ROOT/src/main.cc

[ -f "$SRC" ] || { echo "**找不到 $SRC**" >&2; exit 1; }
[ -f "$LIBTOKENIZER" ] || { echo "**找不到 $LIBTOKENIZER**（LIBTOKENIZER 指错了？）" >&2; exit 1; }
[ -d "$ROOT/include" ] || { echo "**找不到 $ROOT/include**" >&2; exit 1; }
[ -f "$LIBDIR/librknn3_api.so" ] || {
  echo "**$LIBDIR/librknn3_api.so 不在**：TARGET_ARCH 指错了？" >&2; exit 1; }

# 把随仓库带上的运行库铺到 $INSTALL_DIR/lib（rpath $ORIGIN/lib + LD_LIBRARY_PATH=./lib）。
mkdir -p "$INSTALL_DIR/lib"
for lib in "$LIBDIR/librknn3_api.so" "$LIBDIR/librknn3_api_rkcp.so" \
           "$ROOT/third_party/librga/Linux/$ARCH/librga.so"; do
  [ -f "$lib" ] && cp "$lib" "$INSTALL_DIR/lib/"
done

STAMP=$(mktemp)
trap 'rm -f "$STAMP"' EXIT
TMP=$ROOT/build/rknn_multicard_demo.native.$$
mkdir -p "$ROOT/build"
trap 'rm -f "$STAMP" "$TMP"' EXIT

echo "=== main.cc md5 ==="
md5sum "$SRC"

echo "=== 编译（-O2 -s，工程 Release 口径）==="
g++ -std=c++11 -O2 -s -pthread -o "$TMP" "$SRC" \
    -I"$ROOT/include" \
    -L"$INSTALL_DIR/lib" -lrknn3_api \
    "$LIBTOKENIZER" -ldl \
    -Wl,-rpath,'$ORIGIN/lib'

echo "=== 检查 1/3：产物是本次构建新生成的 ==="
if [ ! -f "$TMP" ] || [ ! "$TMP" -nt "$STAMP" ]; then
  echo "**没有产出新的 $TMP —— 编译器应该已经报错了，往上翻。**" >&2
  exit 1
fi

echo "=== 检查 2/3：动态依赖不得出现 libasan/libtsan ==="
NEEDED=$(readelf -d "$TMP" | grep -i 'NEEDED' || true)
echo "$NEEDED"
if echo "$NEEDED" | grep -qiE 'lib(asan|tsan|ubsan)'; then
  echo "**产物链上了 sanitizer 运行时；吞吐数字会失真，不能用。**" >&2
  exit 1
fi

echo "=== 检查 3/3：用法行与本次的改动都在里面 ==="
# 每加一个"只有新后端才有"的字面量就往这里加一条：装上旧二进制时，报错要在这里，
# 而不是等到板卡上某个请求返回 503。
#   `STAT `        资源观测帧的类型行（2026-09-20）——旧网关不认这个标签，会整流错位
#   `node_min_free` 同一批的字段名，比 'STAT ' 更专有（后者会匹配到一堆无关行）
#   `STOP %llu` / `RESUME %llu` / `bad resume header`  暂停/继续那两帧（2026-09-20）——
#     用 sscanf 的格式串与唯一的 ERR 文案当判据，比光写 `STOP `/`RESUME ` 更专有
#   改动 A/B/C（2026-09-23）：`COUNT %llu`（精确计数帧）、`COUNTED `、`SAVED `、
#     `LOADED `、`bad kv op header`（KV 换页）——旧后端没有这些，装了新的网关会对旧
#     后端发 COUNT 而超时，必须在装二进制这一步就拦住。
for pat in '--serve-fd' 'REJECT' 'prompt too long' 'STAT ' 'node_min_free' \
           'STOP %llu' 'RESUME %llu' 'bad resume header' \
           'COUNT %llu' 'COUNTED' 'SAVED' 'LOADED' 'bad kv op header'; do
  n=$(strings "$TMP" | grep -c -- "$pat" || true)
  echo "  '$pat' 出现 $n 次"
  if [ "$n" -eq 0 ]; then
    echo "**产物里找不到 '$pat'：装的不是这一份 main.cc。**" >&2
    exit 1
  fi
done

echo "=== 安装到 $OUT ==="
if [ -f "$OUT" ] && [ "$KEEP_BAK" = 1 ]; then
  BAK="$OUT.bak_$(md5sum "$OUT" | cut -c1-8)"
  cp "$OUT" "$BAK"
  echo "  旧产物另存：$BAK"
fi
cp "$TMP" "$OUT"
md5sum "$OUT"
echo "=== SERVE NATIVE BUILD DONE（重启网关才会用上：./restart_gateway.sh）==="
