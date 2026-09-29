#!/usr/bin/env bash
# 构建 --serve 版执行器（**Release，不带任何 sanitizer**），产物默认另存为
# 项目根目录下的 rknn_multicard_demo.serve。
#
# 构建时直接调用本仓库根目录的 CMakeLists.txt。SDK 的两个预编译产物必须由调用方给出：
#   LIBRKNNAPI    （librknn3_api.so）
#   LIBTOKENIZER  （libtokenizer.a）
#   LIBRKNNAPI_RKCP（可选，librknn3_api_rkcp.so）
#
# 为什么要单独一个 Release 脚本：sanitizer 构建是 Debug + -fsanitize=thread，
# 出来的二进制慢一个量级，用它测吞吐等于白测。板上验收过的 3.33x 是 Release 的数，
# 网关这边的聚合吞吐必须和它同口径才能比。
#
# 为什么先 rm 掉构建目录：CMake 缓存里带着上次 configure 的编译选项，
# CXXFLAGS 覆盖不掉缓存（被这个坑过）。宁可多花几分钟全量重编。
#
# 为什么必须 `set -e`（2026-09-16 修）：这个脚本结尾会把产物拷成
# rknn_multicard_demo.serve，而 start_gateway.sh 用的就是这个文件。以前没有 set -e，
# 编译器报错之后脚本照跑到底：`ls -l` 列的是**上一轮的旧二进制**（构建失败时它还在
# 原地），最后 `cp` 把旧二进制覆盖到 .serve 上，并打出一行 `SERVE BUILD DONE`。
# 也就是说"构建失败"和"构建成功"的输出长得一模一样，而部署出去的是旧代码。
# 光靠 set -e 还不够——它管不到"命令返回 0 但结果是错的"，所以下面的产物检查改成
# 显式断言（时间戳必须晚于本次构建开始、必须没有 sanitizer 依赖）。
#
# 环境变量：
#   LIBRKNNAPI / LIBTOKENIZER / LIBRKNNAPI_RKCP  SDK 预编译库路径（必填/可选，见上）
#   GCC_COMPILER  交叉编译器前缀（如 aarch64-linux-gnu）；不设 = 原生编译
#   OUT           产物另存路径；默认 = 项目根/rknn_multicard_demo.serve
#
# 用法：
#   LIBRKNNAPI=/path/librknn3_api.so LIBTOKENIZER=/path/libtokenizer.a \
#   GCC_COMPILER=aarch64-linux-gnu bash build_serve.sh
set -eu
set -x
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)

# Rockchip 预编译库默认取本仓库 third_party/ 下的 aarch64 版本；要换架构或换 SDK
# 构建，用 TARGET_ARCH / LIBRKNNAPI / LIBRKNNAPI_RKCP / LIBRGA / LIBTOKENIZER 覆盖。
ARCH=${TARGET_ARCH:-aarch64}
LIBRKNNAPI=${LIBRKNNAPI:-$ROOT/third_party/rknpu3/Linux/$ARCH/librknn3_api.so}
LIBRKNNAPI_RKCP=${LIBRKNNAPI_RKCP:-$ROOT/third_party/rknpu3/Linux/$ARCH/librknn3_api_rkcp.so}
LIBRGA=${LIBRGA:-$ROOT/third_party/librga/Linux/$ARCH/librga.so}
LIBTOKENIZER=${LIBTOKENIZER:-$ROOT/third_party/tokenizer/lib/Linux/$ARCH/libtokenizer.a}
GCC_COMPILER=${GCC_COMPILER:-}
BUILD_DIR=${BUILD_DIR:-$ROOT/build/serve}
# 安装布局必须与 start_gateway.sh 的 INSTALL_DIR 约定一致：
#   $INSTALL_DIR/rknn_multicard_demo        可执行文件
#   $INSTALL_DIR/lib/librknn3_api.so        运行时库（rpath $ORIGIN/lib + LD_LIBRARY_PATH=./lib）
INSTALL_DIR=${INSTALL_DIR:-$ROOT/install/rknn_multicard_demo}

cd "$ROOT" || exit 1
echo "=== main.cc md5 ==="
md5sum src/main.cc

# 记下"本次构建开始"的时刻：产物必须比它新，否则说明这次根本没重新链接成功。
STAMP=$(mktemp)
trap 'rm -f "$STAMP"' EXIT

rm -rf "$BUILD_DIR" "$INSTALL_DIR"
unset CFLAGS CXXFLAGS LDFLAGS

CMAKE_ARGS=(
  -S "$ROOT" -B "$BUILD_DIR"
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR"
  -DTARGET_ARCH="$ARCH"
  -DLIBRKNNAPI="$LIBRKNNAPI"
  -DLIBTOKENIZER="$LIBTOKENIZER"
)
[ -n "$LIBRKNNAPI_RKCP" ] && CMAKE_ARGS+=(-DLIBRKNNAPI_RKCP="$LIBRKNNAPI_RKCP")
[ -n "$LIBRGA" ] && CMAKE_ARGS+=(-DLIBRGA="$LIBRGA")
if [ -n "$GCC_COMPILER" ]; then
  CMAKE_ARGS+=(
    -DCMAKE_SYSTEM_NAME=Linux
    -DCMAKE_SYSTEM_PROCESSOR=aarch64
    -DCMAKE_C_COMPILER="${GCC_COMPILER}-gcc"
    -DCMAKE_CXX_COMPILER="${GCC_COMPILER}-g++"
  )
fi

cmake "${CMAKE_ARGS[@]}"
cmake --build "$BUILD_DIR" -j"$(nproc)"
cmake --install "$BUILD_DIR"

B="$INSTALL_DIR/rknn_multicard_demo"
echo "=== 产物 ==="
ls -l "$B"
md5sum "$B"

echo "=== 检查 1/3：产物是本次构建新生成的 ==="
if [ ! -f "$B" ] || [ ! "$B" -nt "$STAMP" ]; then
  echo "**构建没有产出新的 $B —— 编译器应该已经报错了，往上翻。**" >&2
  exit 1
fi

echo "=== 检查 2/3：动态依赖不得出现 libasan/libtsan ==="
READELF=${GCC_COMPILER:+${GCC_COMPILER}-}readelf
NEEDED=$("$READELF" -d "$B" | grep -i 'NEEDED' || true)
echo "$NEEDED"
if echo "$NEEDED" | grep -qiE 'lib(asan|tsan|ubsan)'; then
  echo "**产物链上了 sanitizer 运行时；吞吐数字会失真，不能用。**" >&2
  exit 1
fi

echo "=== 检查 3/3：用法行带上了本次的改动（--serve-fd / REJECT）==="
for pat in '--serve-fd' 'REJECT'; do
  n=$(strings "$B" | grep -c -- "$pat" || true)
  echo "  '$pat' 出现 $n 次"
  if [ "$n" -eq 0 ]; then
    echo "**产物里找不到 '$pat'：装的不是这一次的 main.cc。**" >&2
    exit 1
  fi
done

echo "=== 另存一份（名字与 start_gateway.sh 的默认 B 一致）==="
OUT=${OUT:-$ROOT/rknn_multicard_demo.serve}
cp "$B" "$OUT"
md5sum "$OUT"
echo "=== SERVE BUILD DONE ==="
