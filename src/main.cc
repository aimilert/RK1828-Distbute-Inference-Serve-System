// Copyright (c) 2026 by Rockchip Electronics Co., Ltd. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "Tokenizer.h"
#include "float16.h"
#include "rknn3_api.h"

#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <termios.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <condition_variable>
#include <deque>
#include <functional>
#include <inttypes.h>
#include <iostream>
#include <map>
#include <locale.h>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <wchar.h>
#include "nlohmann/json.hpp"

#define LOGW(fmt, ...) printf("\033[33m" fmt "\033[0m", ##__VA_ARGS__)
#define VLOG(fmt, ...) do { if (g_verbose) printf(fmt, ##__VA_ARGS__); } while (0)

static size_t g_stage_count = 2;
static uint64_t g_bucket_size = 128;
static bool g_verbose = false;
static bool g_ignore_eos = false;
static bool g_interactive = false;
static bool g_performance_mode = false;
static bool g_tensor_dump_enabled = false;
// --dump-tokens 的输出文件；nullptr 表示不导出。单会话路径用它；
// 并发路径改用每个 Conversation 自己的 dump 文件（见 LastStageResultState::token_dump），
// 否则 N 个会话的 token 会在同一个文件里交错。
static FILE* g_token_dump = nullptr;
static std::string g_tensor_dump_dir;
static std::mutex g_tensor_dump_mutex;
static uint64_t g_embed_dump_count = 0;
static uint64_t g_input_dump_count = 0;
// 并发模式下关掉逐 token 的 stdout 打印：N 个会话同时往 stdout 写必然交错，
// 而且 printf 在热路径上会污染吞吐测量。token 走各自的 dump 文件。
static bool g_suppress_generation_output = false;
// Tokenizer 是共享的，且底层实现（3rdparty 预编译库）没有线程安全承诺——decode 路径
// 里的 TokenToPiece/Decode 由 N 个会话线程同时调用，这里串行化，代价可忽略
// （一次调用是微秒级，相比 80ms/token 的推理可以不计）。
static std::mutex g_tokenizer_mutex;

// 用模型自己的 tokenizer 数一段文本的 token 数，失败返回 -1。
// 两个调用点：满上下文探针要报出**真实** token 数（靠「字符数 / 3.5」估算无从判断
// 到底有没有把上下文填满，测试强度就不可知）；服务模式下每一轮派发前也要数一遍
// **本轮 prompt**，那是"装不下就发 REJECT"这条判据的输入（见会话驱动循环）。
// 锁必须在函数里取：服务模式下这是 4 条会话线程的并发调用点，而 Tokenizer
// 没有线程安全承诺（理由就是上面那把锁的注释）；探针路径是单线程的，取锁不要钱。
static int count_prompt_tokens(Tokenizer* tokenizer, const std::string& text)
{
  if (!tokenizer || text.empty()) {
    return -1;
  }
  std::vector<int32_t> buf(text.size() + 16);
  std::lock_guard<std::mutex> tokenizer_lock(g_tokenizer_mutex);
  int n = tokenizer->Tokenize(text.c_str(), (int32_t)text.size(), buf.data(),
                              (int32_t)buf.size());
  return n > 0 ? n : -1;
}

// 交互式多会话把整段回复一次性打到 stdout。这把锁把 printf 与紧跟的 fflush 绑成
// 一个整体，保证「一块回复」在一次持锁内完整落地，块与块之间的先后是可预期的。
// （严格说，glibc 的 printf 本身对同一个 FILE 也持锁，单次调用不会被另一条线程从
// 中间切开；这里加锁是为了把 printf+fflush 绑在一起、并让顺序确定，不是为了修
// 一个已被证实存在的撕裂。）
// 注意它挡不住 run_chat_turn 内部那几处裸 printf 的诊断信息插在块与块之间，
// 那是失败路径，见交互驱动里的说明。
static std::mutex g_output_mutex;

// ============================================================================
// --serve：板端 OpenAI 网关的后端子进程协议。
//
// 为什么单开一个 fd 而不是复用 stdout：这条协议是**帧流**（长度前缀 + 原始字节），
// 任何杂散字节都会让后面所有帧错位。而本文件与 SDK 里有大量裸 printf
// （"prefill failed"、SDK 自己的 session 日志、VLOG…），stdout 无论如何都不干净。
// 所以帧只走一个单独的 fd（默认 3，可用 --serve-fd 改；网关用管道传进来），
// stdout 继续当日志用，两边互不干扰。该 fd 打不开时退回 stdout 并警告——能跑，
// 但不保证不被日志插花。
//
// 帧语法（全部以 '\n' 结束头行，后跟 header 里声明长度的原始字节，不做转义）：
//   启动横幅 (fd): READY <nsessions> <default_max_new_tokens>
//   请求 (stdin):   REQ <rid> <session| -1> <max_new_tokens> <reset> <prompt_len>
//                   <prompt_len 字节 UTF-8 原文>
//                   STAT                              （无载荷，资源观测，见 serve_stat）
//                   STOP <rid>                        （停掉在跑的那一轮，见下）
//                   RESUME <rid> <session> <max_new_tokens>
//                                                     （继续上一轮被打断的 decode）
//                   QUIT
//   输出 (fd3):     DELTA <rid> <n>            / <n 字节原文>
//                   DONE  <rid> <session> <finish_reason> <prefill_tok> <decode_tok>
//                         <prefill_ms> <decode_ms> <ctx_tok>
//                   CLEAR <rid> <session> <ctx_tok> <ctx_limit>
//                   ERR   <rid> <session> <n>  / <n 字节原文>
//                   REJECT <rid> <session> <n> / <n 字节原文>
//                   STAT  <n>                  / <n 字节 JSON（{now_us, cards:[…]}）
//
//   ⚠️ STAT 是**新加的帧类型**，所以这一版的后端要与**同一版的网关**配对：
//   旧网关的读线程不认识 STAT，不会去消费它声明的那 <n> 个字节，整条帧流从此错位
//   （最坏读线程直接死 → 全 503）。这条坑已经咬过一次（ERR 帧那次），所以别只换一半。
//
//   STOP 的语义（2026-09-20 加，配合网页端的「暂停回答」）：
//     · **就地处理、不进调度队列**——和 STAT 同一个理由，而且这里更绝对：排到队尾
//       的话，它前面正好是它想停的那条长 decode（那个会话被钉着），等它跑完再去
//       停就等于没停。
//     · 只置一个标志位，**真正停手发生在下一个 token 边界上**（run_chat_turn 的
//       decode 循环顶上）。板上约 10 tok/s ⇒ 最坏 0.1 s 之后停。
//     · **停不了 prefill**：一轮 prefill 是一次原子的 session_run。标志位只能在本轮
//       prefill **之前**生效（那种情况整轮不发 token）。这是故意的——用
//       rknn3_session_stop 去砍 prefill 会把 KV 停在半截，而网关的 known[] 以为整段
//       都在，下一轮就会发增量拼出一段没有开头的对话：答得通顺但是错的。
//     · 停了**照发 DONE**（finish_reason=abort，decode_tok = 真跑出来的个数）。
//       这一点很关键：KV 里确实装着「本轮 prompt + 已生成的这些 token」，
//       conv.context_tokens 与网关的 known[] 都按这个数记账，三方一致，下一轮
//       接着问 / 继续问都对得上。
//     · rid 不认识（那一轮已经跑完，或根本不存在）= 静默无事发生。**不回帧**：
//       多一种状态就要多一条网关分支，而有没有停到，从 DONE 本来就看得出来。
// ============================================================================
static bool  g_serve_mode = false;
static FILE* g_serve_out  = nullptr;

// 帧通道的 fd 由 --serve-fd 指定（默认 3）。做成参数而不是写死 3：网关用 pass_fds
// 把管道的写端交给子进程时，子进程拿到的 fd 号就是父进程那个号，父进程没法安全地
// 要求它一定是 3（想强凑 3 就得在 spawn 时 dup2，多线程下那是不安全的玩法）。
// 让子进程按参数认 fd，两边都省事。
static FILE* serve_open_channel(int fd)
{
  FILE* fp = fdopen(fd, "wb");
  if (!fp) {
    fprintf(stderr, "[serve] fd %d is not available; falling back to stdout, "
                    "stray logs may corrupt the frame stream\n", fd);
    fp = stdout;
  }
  return fp;
}

// 所有帧都经这里出：一次持锁写完整帧（头行 + 原始载荷）再 flush，保证帧与帧不交错。
static void serve_frame(const char* tag, const std::string& head,
                        const void* payload, size_t payload_len)
{
  if (!g_serve_out) {
    return;
  }
  std::lock_guard<std::mutex> lock(g_output_mutex);
  fprintf(g_serve_out, "%s%s\n", tag, head.c_str());
  if (payload && payload_len > 0) {
    fwrite(payload, 1, payload_len, g_serve_out);
  }
  fflush(g_serve_out);
}

static void serve_delta(uint64_t rid, const std::string& text)
{
  serve_frame("DELTA ", std::to_string(rid) + " " + std::to_string(text.size()),
              text.data(), text.size());
}

static void serve_done(uint64_t rid, int session, const char* finish_reason,
                       uint64_t prefill_tokens, uint64_t decode_tokens,
                       double prefill_ms, double decode_ms, uint64_t context_tokens)
{
  char head[256];
  snprintf(head, sizeof(head), "%llu %d %s %llu %llu %.1f %.1f %llu",
           (unsigned long long)rid, session, finish_reason,
           (unsigned long long)prefill_tokens, (unsigned long long)decode_tokens,
           prefill_ms, decode_ms, (unsigned long long)context_tokens);
  serve_frame("DONE ", head, nullptr, 0);
}

static void serve_clear(uint64_t rid, int session, uint64_t context_tokens, uint64_t limit)
{
  char head[160];
  snprintf(head, sizeof(head), "%llu %d %llu %llu",
           (unsigned long long)rid, session,
           (unsigned long long)context_tokens, (unsigned long long)limit);
  serve_frame("CLEAR ", head, nullptr, 0);
}

static void serve_err(uint64_t rid, int session, const std::string& message)
{
  serve_frame("ERR ", std::to_string(rid) + " " + std::to_string(session) + " " +
                        std::to_string(message.size()),
              message.data(), message.size());
}

// 后端在 prefill **之前**拒了这一轮：会话是好的，只是这一轮没跑。目前两种来源：
// 「上下文装不下」（带着一帧 CLEAR，那段历史确实被丢了）和「载荷超过帧上限」
// （不带 CLEAR——KV 一个字节都没动，网关那边 known[] 保持原样才是对的）。
//
// 为什么单开一个帧类型而不是复用 ERR：网关对两者的处置**必须**不同。ERR 的含义是
// "该会话的驱动线程已经退出"，网关收到就把这个会话标死、不再往上派活——那是对的。
// 而上下文装不下不是会话的错，标死等于每撞一次超限就永久少一个会话，四次之后整个
// 服务没会话可用。所以 REJECT 让网关只失败这一条请求（HTTP 400）并把粘性记录作废，
// 会话本身照常留在池子里。
static void serve_reject(uint64_t rid, int session, const std::string& message)
{
  serve_frame("REJECT ", std::to_string(rid) + " " + std::to_string(session) + " " +
                           std::to_string(message.size()),
              message.data(), message.size());
}

// 精确 token 计数帧的答复：`COUNTED <rid> <tokens>`（tokens<0 = 数不出来）。
//
// 网关是纯 Python 标准库、手里没有 tokenizer，而"单会话在有界内存里滚动"要先知道
// 一段 prompt 到底占多少 token 才能决定丢多少历史。与其让网关拿字符数瞎估（中英混排
// 误差很大），不如让唯一持有 tokenizer 的后端就地数一次——不碰设备、不占会话、
// 不进调度队列，成本是一次 tokenize（微秒级）。数不出来时回 -1，网关据此退回"不裁剪"，
// 宁可让后端原来的 REJECT 兜底，也不要拿一个错的预算去裁。
static void serve_counted(uint64_t rid, int tokens)
{
  serve_frame("COUNTED ", std::to_string(rid) + " " + std::to_string(tokens), nullptr, 0);
}

// 改动 C：KV 存/取作业的答复：`SAVED <rid> <ok>` / `LOADED <rid> <ok>`（ok=0/1）。
// 网关据此知道这次换页成没成；失败就按"没换过"处理（会话照常被收回，只是下次全量重算）。
static void serve_op_result(const char* tag, uint64_t rid, int ok)
{
  serve_frame(tag, std::to_string(rid) + " " + std::to_string(ok), nullptr, 0);
}

enum class InferencePhase
{
  PREFILL,
  DECODE,
};

struct TensorBlob
{
  rknn3_tensor_attr    attr;
  std::vector<uint8_t> data;
};

struct StageBatch
{
  std::vector<TensorBlob> tensors;
  uint64_t                n_tokens = 0;
};

struct StageSlot
{
  std::mutex              mutex;
  std::condition_variable cv;
  std::deque<StageBatch>  batches;
  uint64_t                expected_tokens = 0;
  uint64_t                emitted_tokens = 0;
  uint64_t                active_input_tokens = 0;
  bool                    producer_done = false;
  bool                    failed = false;
};

struct EmbedCallbackContext;

struct PipelineState
{
  std::vector<std::unique_ptr<StageSlot>> slots;

  explicit PipelineState(size_t stage_count)
  {
    slots.reserve(stage_count);
    for (size_t i = 0; i < stage_count; ++i) {
      slots.emplace_back(new StageSlot());
    }
  }
};

struct embedding_info
{
  int      fd = -1;
  float16* embedding_data = nullptr;
  int      embedding_dim = 0;
  int      vocab_size = 0;
};

struct EmbedCallbackContext
{
  embedding_info* embed_info = nullptr;
  PipelineState*  pipeline = nullptr;
};

struct StageCallbackContext
{
  PipelineState* pipeline = nullptr;
  size_t         stage_index = 0;
  int32_t        embedding_dim = 0;
};

struct PhasePerformanceStatistics
{
  uint64_t run_count = 0;
  uint64_t token_count = 0;
  double   total_time_ms = 0.0;
};

struct StagePerformanceStatistics
{
  PhasePerformanceStatistics prefill;
  PhasePerformanceStatistics decode;
};

// 一张卡（流水线的一段）上所有会话共用的资源：NPU context、输出张量、形状信息。
// 多会话并发时这部分**只有一份**，权重与这部分设备内存天然共享——P0 探针已实测
// 证实（每加一个 session 只多出 KV cache，权重不复制）。
struct StageContext
{
  std::string      name;
  std::string      model_path;
  std::string      weight_path;
  std::string      device_id;
  rknn3_context    ctx = 0;
  int32_t          embedding_dim = 0;
  int32_t          vocab_size = 0;
  int32_t          max_ctx_len = 0;
  rknn3_tensor*    output_tensors = nullptr;
  int              n_output_tensors = 0;
  int*             ext_input_indices = nullptr;
  int              n_ext_inputs = 0;
  // 卡级统计：同一张卡上所有会话的耗时都累加到这里。
  StagePerformanceStatistics performance;

  // 卡级串行锁：同一张卡上同一时刻只允许一个 session 在一次 rknn3_session_run 里跑。
  //
  // 为什么必须有：output_tensors 是**每张卡一份**的设备内存（init_output_tensors 分配），
  // 所有 session 的回调都往同一块 buffer 里读。两个会话在同一张卡上并发跑，
  // 后一个会把前一个的隐状态覆盖掉——采样出来的 token 会错，而且错得不稳定。
  // 持锁范围覆盖整个 session_run，所以回调里对 output_tensors 的读取也在锁内。
  //
  // 这不会牺牲流水线并行：每个会话自己的 4 段仍然各占一张卡并行跑，
  // 被串行化的只是「同一张卡上的不同会话」。而 P0 实测单会话时每卡 decode
  // 利用率只有 25.5%，串行反而正好把这张卡的空闲时间喂满。
  //
  // 这也是方案文档 §3.2 的方案 A。方案 B（每 session 一份 output_tensors，
  // 每份约 1.3 MB）能去掉这把锁，但需要 runtime 支持同 context 真并发，
  // 留待后续验证。
  std::mutex run_mutex;

  // ---- 资源观测（--serve 的 STAT 帧读这几个）----
  //
  // 为什么是"worker 在锁内顺手更新 + 输入线程只读原子量"，而不是输入线程现查设备：
  // 从输入线程直接查就得先拿 run_mutex，而一次 decode 可能跑几十秒——那等于把整个请求
  // 入口堵死，而且恰好堵在"板卡正忙、最想看一眼"的时候。所以设备查询只在已经持有锁的
  // 地方发生（见 refresh_stage_mem_stat），STAT 帧本身零设备访问、零锁等待。
  //
  // `stat_busy_us` 累加的是**卡级锁内**跑 session_run 的微秒数，也就是这张卡真正在算的
  // 时间（等锁的时间不算，那段是别的会话在占着它）。两次采样之差 ÷ 墙上时间 = 该卡
  // NPU 的利用率。这是**量出来的**，不是估的。
  //
  // 内存那几项是**快照**不是累计：KV 在 session_init 时按满上下文一次性预分配，之后
  // 填满上下文新增分配 0.0 MB（§4.1 实测），所以一次会话集合确定后整卡空闲量就不再变，
  // 刷新一次就够准。`stat_mem_at_us` 是采样时刻，网关据此说出这个数有多旧。
  std::atomic<uint64_t> stat_busy_us{0};
  std::atomic<uint64_t> stat_run_calls{0};
  // stage0 实时结算用（其余段用不到，保持 0 即可）。stat_busy_base_us 是本次 run
  // 开始前的累计忙时，stat_live_start_us 是本次 run 的单调起点（0=当前没有在跑）。
  // embed_callback 在 run 进行中把 `base + (now - live_start)` 增量写进 stat_busy_us，
  // 于是长 prefill 也能被页面看见；run 返回时再由 record_stage_run_stat_absolute
  // 把同一段时长**绝对写回**（不是 fetch_add，避免与增量重复计）。
  std::atomic<uint64_t> stat_busy_base_us{0};
  std::atomic<uint64_t> stat_live_start_us{0};
  std::atomic<uint64_t> stat_mem_at_us{0};    // 0 = 一次都还没采过
  // ⚠️ 这两个是**该卡所有 node 的和**，不是 dev_mem.sys_total/sys_free。
  // sys_* 是"本机侧的小块内存"（实测只有 ~19 MB，且它占的是驱动/传输缓冲，与模型
  // 放不放得下无关）——2026-09-20 第一次上板就拿它当整卡内存显示过，页面上每张卡
  // 写"19.0 MB"，而 `rknn-smi` 同一时刻报的是 ~93% 占用。见 refresh_stage_mem_stat。
  std::atomic<uint64_t> stat_mem_total{0};    // Σ node.total（字节）= 整卡总量
  std::atomic<uint64_t> stat_mem_free{0};     // Σ node.free（字节）= 整卡空闲
  std::atomic<uint64_t> stat_node_min_free{0};// 全卡最紧那个 node 的空闲（字节）
  std::atomic<uint32_t> stat_node_num{0};
};

// 一个会话在一张卡上占用的部分：session 句柄 + 注册到它上面的回调。
// RKLLMCallback 必须跟着会话走而不是跟着卡走：它同时携带 per-card 的东西
// （output_tensors / ext_input_indices / tokenizer）和 per-conversation 的东西
// （callback_ctx.pipeline / embed_ctx / result_userdata）。多会话并发时，
// 每个 session 都要有一套指向**自己**那个 Conversation 的回调。
struct StageSession
{
  rknn3_session*   session = nullptr;
  RKLLMCallback    callback;
  bool             has_callback = false;
  StageCallbackContext callback_ctx;
  EmbedCallbackContext  embed_ctx;
};

// 一轮回复文本的落点。做成接口是因为有两种互斥的消费方式：
//   · 交互模式（--interactive）攒进 std::string，跑完整段加 [s<i>] 前缀一次打出；
//   · 服务模式（--serve）每来一段立刻发一帧 DELTA，网关据此做 SSE 流式。
// 契约与原来的 block_out 逐字一致（见下），只是把"攒"换成一次虚调用：
// 每 token 一次，相对 80ms/token 的推理开销可忽略。
struct TokenSink
{
  virtual ~TokenSink() {}
  virtual void on_piece(const std::string& piece) = 0;
};

// 整段成块用：文本落在自己持有的 std::string 上。
struct StringTokenSink : TokenSink
{
  std::string text;
  void on_piece(const std::string& piece) override { text.append(piece); }
};

// 流式用：不攒文本，每段直接发一帧。整段内容由网关侧按 SSE 语义累加。
struct ServeTokenSink : TokenSink
{
  uint64_t rid;
  explicit ServeTokenSink(uint64_t request_id) : rid(request_id) {}
  void on_piece(const std::string& piece) override { serve_delta(rid, piece); }
};

struct LastStageResultState
{
  Tokenizer* tokenizer = nullptr;
  std::mutex mutex;
  bool       has_token = false;
  int32_t    next_token = -1;
  // 会话私有的 token dump 文件。非空时优先于全局 g_token_dump。
  // 并发模式下每会话一个文件，避免 N 路 token 交错成一个文件。
  FILE*      token_dump = nullptr;
  // 本轮回复的落点（见上面的 TokenSink）。非空时回调把本会话这一轮生成的文本交给它，
  // 而不是逐 token 打到 stdout（N 路并发的逐 token 打印会互相穿插）。
  //
  // 它指向对话驱动线程栈上的一个 sink 对象，跨线程传递，所以这里必须写清楚
  // 为什么不会用到已析构的对象——**注意不是"同一个线程"**：
  //   · 每个 stage 的 result_callback 都挂着同一个 &conv.result（init 里统一赋值），
  //     而 stage 1..N-1 跑在 run_pipeline_once 临时起的 worker 线程上，不是驱动线程。
  //   · 但只有最后一级会真正产出采样 token：infer_param.disable_sampling 默认为 true，
  //     run_stage_worker 里只有 is_last_stage 才在最后一个桶上把它打开。所以真正
  //     会 on_piece 的只有最后一级那一条线程。
  //   · 驱动线程在调用 run_pipeline_once **之前**写 out（创建 worker 线程之前，
  //     写操作对它们可见）；run_pipeline_once 返回**之前** join 了所有 worker 线程，
  //     驱动线程之后才清空它。两侧各有一条 happens-before 边。
  // 也就是说这个安全性的前提是：回调是同步的、stage worker 不跨轮复用、
  // run_pipeline_once 不会提前返回。这三条任何一条被改掉，out 立刻变成
  // 指向已析构栈对象的悬垂指针——改动这条路径时请先回来看这段注释。
  TokenSink* out = nullptr;
};

// 一个独立会话：自己的一整套 session、流水线队列、结果槽与统计。
// 单会话路径创建一个（行为与改造前完全一致）；--sessions N 时创建 N 个并发驱动。
struct Conversation
{
  std::vector<StageSession> stages;    // 每卡一个 session
  PipelineState             pipeline;
  LastStageResultState      result;
  // 当前对话累计占用上下文 token 数（prefill + decode），用于接近上限时清 KV。
  uint64_t                  context_tokens = 0;
  bool                      first_turn = true;
  uint64_t                  total_prefill_tokens = 0;
  uint64_t                  total_decode_tokens = 0;
  double                    total_prefill_ms = 0.0;
  double                    total_decode_ms = 0.0;

  // 并发模式专用：错误标记、墙钟耗时（含等锁与等流水线的时间，
  // 与 total_decode_ms 里的纯 NPU 时间不同——后者只统计 session_run 本身）。
  //
  // 这里原本有个 `int index`（会话编号）：全文件只有并发基准那一条路径给它赋过值
  // （`conv->index = s`），**没有任何读点**——真正需要编号的地方（打印、派发、
  // `[s<i>]` 前缀）用的都是 driver 的参数 `index` 或会话在 vector 里的下标。
  // 有写没读的字段比没有字段更坏：它让人以为"Conversation 自己知道我是几号"，
  // 而实际值取决于路径（服务/交互路径下永远是 0）。2026-09-16 连同那句赋值一起删。
  bool                      failed = false;
  double                    wall_ms = 0.0;
  // 交互式多会话专用：本会话处理完成的轮数。
  int                       turns_done = 0;

  explicit Conversation(size_t stage_count) : stages(stage_count), pipeline(stage_count) {}
};

struct rope_cache_tensor
{
  void* data = nullptr;
  int   n_dims = 0;
  int   shape[5] = {};
  int   dtype = 0;
  int   layout = 0;
};

enum class RopeCacheFormat
{
  NONE = 0,
  GEMMA4,
  QWEN35,
};

static const char* GEMMA4_ROPE_CACHE_NAMES[4] = {
    "rope_cos_cache_0", "rope_sin_cache_0",
    "rope_cos_cache_1", "rope_sin_cache_1"
};

static const char* QWEN35_ROPE_CACHE_NAMES[2] = {
    "rope_cos_cache", "rope_sin_cache"
};

// 多轮对话的 prompt 拼装模板。Qwen3.5 与 Gemma-4 的对话标记完全不同，不能共用；
// 另注意 Gemma 没有 system role，首轮不能拼 system prompt。
enum class ChatTemplateFamily
{
  QWEN35 = 0,
  GEMMA4,
};

struct ChatTemplateSpec
{
  const char* system_prompt;
  const char* user_prefix;
  const char* user_postfix;
};

static const ChatTemplateSpec QWEN35_CHAT_TEMPLATE = {
    "<|im_start|>system\nYou are Qwen, created by Alibaba Cloud. You are a helpful assistant.<|im_end|>\n",
    "<|im_start|>user\n",
    "<|im_end|>\n<|im_start|>assistant\n",
};

// Gemma-4 官方模板，取自模型导出时生成的 config.pkl（system_prompt / prompt_prefix /
// prompt_postfix 三个字段），与 examples/gemma4 的 set_chat_template 一致。
// 注意：Gemma-4 用的是 <|turn> / <turn|>，与 functiongemma 的 <start_of_turn> /
// <end_of_turn> 完全不同，混用会让模型既进不了 channel、也不在回合结束处停止。
// postfix 末尾预置空 thought channel，模型据此跳过思考直接作答，输出更干净。
static const ChatTemplateSpec GEMMA4_CHAT_TEMPLATE = {
    "",
    "<|turn>user\n",
    "<turn|>\n<|turn>model\n<|channel>thought\n<channel|>",
};

static const ChatTemplateSpec* chat_template_for(ChatTemplateFamily family)
{
  return family == ChatTemplateFamily::GEMMA4 ? &GEMMA4_CHAT_TEMPLATE : &QWEN35_CHAT_TEMPLATE;
}

static const char* chat_template_name(ChatTemplateFamily family)
{
  return family == ChatTemplateFamily::GEMMA4 ? "gemma4" : "qwen3.5";
}

struct InputCbUserdata
{
  rope_cache_tensor rope_caches[4];
  RopeCacheFormat   rope_format = RopeCacheFormat::NONE;
  int               rope_fd = -1;
  void*             rope_mmap_base = nullptr;
  size_t            rope_mmap_size = 0;
};

static int find_rope_cache_index(RopeCacheFormat format, const char* name)
{
  const char* const* names = nullptr;
  int                count = 0;
  if (format == RopeCacheFormat::GEMMA4) {
    names = GEMMA4_ROPE_CACHE_NAMES;
    count = 4;
  } else if (format == RopeCacheFormat::QWEN35) {
    names = QWEN35_ROPE_CACHE_NAMES;
    count = 2;
  }

  for (int i = 0; i < count; ++i) {
    if (strcmp(name, names[i]) == 0) {
      return i;
    }
  }
  return -1;
}

static size_t get_dtype_elem_size(int dtype)
{
  switch (dtype) {
  case 0:  return 4;   /* FLOAT32   */
  case 1:  return 2;   /* FLOAT16   */
  case 2:  return 1;   /* INT8      */
  case 3:  return 1;   /* UINT8     */
  case 4:  return 2;   /* INT16     */
  case 5:  return 2;   /* UINT16    */
  case 6:  return 4;   /* INT32     */
  case 7:  return 4;   /* UINT32    */
  case 8:  return 8;   /* INT64     */
  case 9:  return 8;   /* UINT64    */
  case 10: return 1;   /* BOOL      */
  case 11: return 1;   /* INT4      */
  case 12: return 1;   /* FLOAT8E4M3FN */
  case 13: return 2;   /* BFLOAT16  */
  case 14: return 1;   /* FLOAT8E8M0   */
  case 15: return 1;   /* FLOAT4E2M1   */
  default: return 1;
  }
}

static double elapsed_us(const timeval& start, const timeval& end)
{
  return (end.tv_sec - start.tv_sec) * 1e6 + (end.tv_usec - start.tv_usec);
}

// 资源观测用的单调时钟（微秒）。**不用 gettimeofday**：利用率是两次采样的差值之比，
// 中间被 NTP 校一次表，epoch 时钟会算出一个荒谬的比值（甚至负数）。
static uint64_t stat_now_us()
{
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}

// 每卡内存快照的刷新间隔。2s：够页面看着是"实时"，又不至于让每轮 decode 都多一次
// 设备查询（那会实打实地吃掉吞吐）。
static const uint64_t kStatRefreshUs = 2000000ull;

// 采集一张卡的内存快照，写进 StageContext 的原子量（--serve 的 STAT 帧读它们）。
//
// **调用者必须持有 stage.run_mutex**，或者处于"还没有并发跑起来"的初始化阶段：
// rknn3_query 与 rknn3_session_run 走的是同一条到设备的传输通道，SDK 没承诺两者
// 并发安全，而踩坏这条通道的代价是四张卡僵死（见 --probe-sessions 那一段的记录）。
// 这里故意**不自己加锁**——worker 是在锁内顺势调用的，再锁一次就是死锁。
static void refresh_stage_mem_stat(StageContext& stage)
{
  rknn3_dev_mem_info dev_mem;
  memset(&dev_mem, 0, sizeof(dev_mem));
  if (rknn3_query(stage.ctx, RKNN3_QUERY_DEVICE_MEM_INFO, &dev_mem, sizeof(dev_mem)) !=
      RKNN3_SUCCESS) {
    // 查不到就保留上一次的快照。**不更新采样时刻**，这样网关能看出这个数旧了——
    // 宁可显示"这个数有 12s 没更新过"，也不要拿旧值冒充新的。
    return;
  }
  const uint32_t n = dev_mem.node_num < RKNN3_MAX_NPU_NODE_NUM ? dev_mem.node_num
                                                              : RKNN3_MAX_NPU_NODE_NUM;
  // 整卡量 = 各 node 之和；**不能用 sys_total/sys_free**：那两个是"本机侧的小块内存"
  // （同一个结构体里的另一组字段，实测 ~19 MB 量级，见 --probe 那段的老注释），拿它当
  // 整卡内存显示出来的是个假数。node 才是模型真正落地的内存：8 个 node 各 ~634 MB，
  // 加起来 ~5071 MB/卡，跟 `rknn-smi` 的占用率对得上（2 路 32K ⇒ 93/95%）。
  uint64_t sum_total = 0, sum_free = 0;
  uint64_t tightest = 0;
  bool     have_tightest = false;
  for (uint32_t i = 0; i < n; ++i) {
    const uint64_t node_total = dev_mem.node_mem_info[i].total;
    const uint64_t node_free = dev_mem.node_mem_info[i].free;
    sum_total += node_total;
    sum_free += node_free;
    if (!have_tightest || node_free < tightest) {
      tightest = node_free;
      have_tightest = true;
    }
  }
  stage.stat_mem_total.store(sum_total, std::memory_order_relaxed);
  stage.stat_mem_free.store(sum_free, std::memory_order_relaxed);
  stage.stat_node_min_free.store(have_tightest ? tightest : 0, std::memory_order_relaxed);
  stage.stat_node_num.store(n, std::memory_order_relaxed);
  stage.stat_mem_at_us.store(stat_now_us(), std::memory_order_relaxed);
}

// 一次 session_run 回来之后的资源观测：忙时累计 + 顺手刷一次内存快照。
//
// **调用者必须持有该卡的 run_mutex**（与 refresh_stage_mem_stat 同样的理由），并且
// 传进来的 busy_us 必须就是这一次 run 的时长——忙时最后要跟 STAT 的 `now_us` 相除算
// 利用率，量到别的区间（比如"等锁的那一段"）就等于把排队算成了算力。
//
// ⚠️ 这个函数有**两个**调用点，都要调：run_stage_worker 里的各段，以及
// run_pipeline_once 里就地跑的 stage0（stage0 不走 worker）。2026-09-20 就是漏了后者
// ——主机测试 t12 抓到的：第 0 张卡永远报 run_calls=0、内存永远"还没采到"，页面上
// 一张卡一直空着，而另外三张在动。所以这里收成一个函数，而不是把三行抄两遍。
static void record_stage_run_stat(StageContext& stage, uint64_t busy_us)
{
  stage.stat_busy_us.fetch_add(busy_us, std::memory_order_relaxed);
  stage.stat_run_calls.fetch_add(1, std::memory_order_relaxed);
  if (stat_now_us() - stage.stat_mem_at_us.load(std::memory_order_relaxed) >= kStatRefreshUs) {
    refresh_stage_mem_stat(stage);
  }
}

// stage0 专用：与 record_stage_run_stat 的区别是这里传的是**含本次 run 的累计忙时**，
// 所以直接 store 而不是 fetch_add——运行中 embed_callback 已经把这些秒数增量写进去了，
// 再 add 一次就把同一段时长算了两遍。
//
// 为什么 stage0 需要它：prefill 在 stage0 是**一次** rknn3_session_run 跑完整段 prompt
// （embed_callback 在期间逐桶派发给下游）。若沿用「返回时才结算」，整个 prefill 期间
// stat_busy_us 一动不动，页面滑窗一除就是 0%——而同一时刻下游各段因为**逐桶**调用而
// 报满 100%，于是出现「#0=0%、#1/#2=100%」这种看起来像 0 号卡没上工的假象。
static void record_stage_run_stat_absolute(StageContext& stage, uint64_t accumulated_busy_us)
{
  stage.stat_busy_us.store(accumulated_busy_us, std::memory_order_relaxed);
  stage.stat_run_calls.fetch_add(1, std::memory_order_relaxed);
  if (stat_now_us() - stage.stat_mem_at_us.load(std::memory_order_relaxed) >= kStatRefreshUs) {
    refresh_stage_mem_stat(stage);
  }
}

// 资源观测帧。请求是 stdin 上的一行 `STAT`（不带载荷、不带 rid），答复是
//   `STAT <len>` + <len 字节 JSON>
//
// 为什么载荷用 JSON 而不是像别的帧那样排空格：这一帧是**嵌套**的（每张卡一组数），
// 排空格就得先定死字段序、以后加一个字段两边一起改；而两边本来就都在处理 JSON
// （C++ 侧 nlohmann，网关侧标准库 json），用 JSON 之后加字段是纯加法。
//
// **这一帧只读原子量、不碰设备、也不进调度队列**（理由见 StageContext 里 stat_busy_us
// 的注释）：它得在一次长 decode 正跑着的时候也能立刻回答——那正是最需要它的时候。
static void serve_stat(const std::vector<StageContext>& stages)
{
  nlohmann::json doc;
  doc["now_us"] = stat_now_us();
  nlohmann::json cards = nlohmann::json::array();
  const uint64_t now_us = stat_now_us();
  for (const auto& stage : stages) {
    nlohmann::json card;
    card["name"] = stage.name;
    // 忙时要含「正在跑、还没返回」的那一段：prefill 在 stage0 是**一次**长 session_run，
    // 只在返回时结算等于让整段 prefill 被读成 0%（下游各段因逐桶调用反而报满 100%，
    // 于是出现「#0=0%、#1/#2=100%」的假象）。live_start=0 表示当前没有在跑，就用已结算值；
    // 否则用 base + (now - live_start) 合成——只读原子量与时钟，不碰设备、不进调度队列，
    // 与 STAT 帧原本的契约一致。
    uint64_t busy_us = stage.stat_busy_us.load(std::memory_order_relaxed);
    const uint64_t live_start = stage.stat_live_start_us.load(std::memory_order_relaxed);
    if (live_start != 0) {
      busy_us = stage.stat_busy_base_us.load(std::memory_order_relaxed) + (now_us - live_start);
    }
    card["busy_us"] = busy_us;
    card["run_calls"] = stage.stat_run_calls.load(std::memory_order_relaxed);
    // 快照时刻。**0 = 一次都没采到**，与"采过但很旧"是两回事：前者只能显示"未知"，
    // 拿 0 当时刻去算年龄会得到一个天文数字（页面会显示成"53 年没更新"）。
    card["mem_at_us"] = stage.stat_mem_at_us.load(std::memory_order_relaxed);
    card["mem_total"] = stage.stat_mem_total.load(std::memory_order_relaxed);
    card["mem_free"] = stage.stat_mem_free.load(std::memory_order_relaxed);
    card["node_num"] = stage.stat_node_num.load(std::memory_order_relaxed);
    card["node_min_free"] = stage.stat_node_min_free.load(std::memory_order_relaxed);
    card["ctx_len"] = stage.max_ctx_len;
    cards.push_back(card);
  }
  doc["cards"] = cards;
  const std::string body = doc.dump();
  serve_frame("STAT ", std::to_string(body.size()), body.data(), body.size());
}

static void print_performance_statistics(uint64_t prefill_tokens, float prefill_ms,
                uint64_t decode_tokens, float decode_ms)
{
  float prefill_s = prefill_ms / 1e3f;
  float prefill_tpt = prefill_tokens == 0 ? 0.0f : prefill_ms / (float)prefill_tokens;
  float prefill_tps = prefill_tokens == 0 ? 0.0f : (float)prefill_tokens / prefill_s;

  float decode_s = decode_ms / 1e3f;
  float decode_tpt = decode_tokens == 0 ? 0.0f : decode_ms / (float)decode_tokens;
  float decode_tps = decode_tokens == 0 ? 0.0f : (float)decode_tokens / decode_s;

  printf("\n\nPerformance Statistics: ");
  printf("\n-----------------------------------------------------------------------------------------\n");
  printf(" %-10s | %-16s | %-8s | %-20s | %-20s \n",
    "Stage", "Total Time (ms)", "Tokens", "Time per Token (ms)", "Tokens per Second");
  printf("-----------------------------------------------------------------------------------------\n");
  printf(" %-10s | %-16.2f | %-8llu | %-20.2f | %-20.2f \n",
    "Prefill", prefill_ms, (unsigned long long)prefill_tokens, prefill_tpt, prefill_tps);
  printf(" %-10s | %-16.2f | %-8llu | %-20.2f | %-20.2f \n",
    "Decode", decode_ms, (unsigned long long)decode_tokens, decode_tpt, decode_tps);
  printf("-----------------------------------------------------------------------------------------\n");
}

static void record_stage_performance(StageContext& stage, InferencePhase phase,
                                     uint64_t token_count, double total_time_ms)
{
  PhasePerformanceStatistics& statistics = phase == InferencePhase::PREFILL
                                                ? stage.performance.prefill
                                                : stage.performance.decode;
  statistics.run_count++;
  statistics.token_count += token_count;
  statistics.total_time_ms += total_time_ms;
}

static void print_stage_performance_statistics(const std::vector<StageContext>& stages)
{
  printf("\nPer-Stage Performance Statistics: ");
  printf("\n----------------------------------------------------------------------------------------------------------------------\n");
  printf(" %-10s | %-10s | %-8s | %-16s | %-8s | %-20s | %-20s \n",
         "Stage", "Phase", "Runs", "Total Time (ms)", "Tokens", "Time per Token (ms)", "Tokens per Second");
  printf("----------------------------------------------------------------------------------------------------------------------\n");

  for (const auto& stage : stages) {
    const PhasePerformanceStatistics* phase_statistics[] = {
        &stage.performance.prefill,
        &stage.performance.decode,
    };
    const char* phase_names[] = {"Prefill", "Decode"};

    for (size_t i = 0; i < 2; ++i) {
      const PhasePerformanceStatistics& statistics = *phase_statistics[i];
      double time_per_token = statistics.token_count == 0
                                  ? 0.0
                                  : statistics.total_time_ms / (double)statistics.token_count;
      double tokens_per_second = statistics.total_time_ms <= 0.0
                                     ? 0.0
                                     : (double)statistics.token_count * 1e3 / statistics.total_time_ms;
      printf(" %-10s | %-10s | %-8llu | %-16.2f | %-8llu | %-20.2f | %-20.2f \n",
             stage.name.c_str(), phase_names[i],
             (unsigned long long)statistics.run_count,
             statistics.total_time_ms,
             (unsigned long long)statistics.token_count,
             time_per_token, tokens_per_second);
    }
  }
  printf("----------------------------------------------------------------------------------------------------------------------\n");
}

static void release_safetensors(InputCbUserdata* cb_data)
{
  if (!cb_data) return;
  if (cb_data->rope_mmap_base && cb_data->rope_mmap_base != MAP_FAILED) {
    munmap(cb_data->rope_mmap_base, cb_data->rope_mmap_size);
    cb_data->rope_mmap_base = nullptr;
  }
  if (cb_data->rope_fd >= 0) {
    close(cb_data->rope_fd);
    cb_data->rope_fd = -1;
  }
}

static void release_output_tensors(StageContext& stage)
{
  if (!stage.output_tensors) {
    return;
  }

  for (int i = 0; i < stage.n_output_tensors; ++i) {
    if (stage.output_tensors[i].mem) {
      rknn3_destroy_mem(stage.ctx, stage.output_tensors[i].mem);
      stage.output_tensors[i].mem = nullptr;
    }
    if (stage.output_tensors[i].attr) {
      free(stage.output_tensors[i].attr);
      stage.output_tensors[i].attr = nullptr;
    }
  }

  free(stage.output_tensors);
  stage.output_tensors = nullptr;
  stage.n_output_tensors = 0;
}

// 只销毁会话自己的 session；卡级资源由 destroy_context 负责。
static void destroy_session(StageSession& sess)
{
  if (sess.session) {
    rknn3_session_destroy(sess.session);
    sess.session = nullptr;
  }
  sess.has_callback = false;
}

static void destroy_conversation(Conversation& conv)
{
  for (auto& sess : conv.stages) {
    destroy_session(sess);
  }
}

// 销毁卡级资源。调用前必须确保该卡上所有会话的 session 都已销毁。
static void destroy_context(StageContext& stage)
{
  release_output_tensors(stage);

  if (stage.ext_input_indices) {
    free(stage.ext_input_indices);
    stage.ext_input_indices = nullptr;
  }
  stage.n_ext_inputs = 0;

  if (stage.ctx) {
    rknn3_destroy(stage.ctx);
    stage.ctx = 0;
  }
}

static void destroy_contexts(std::vector<StageContext>& stages)
{
  for (auto& stage : stages) {
    destroy_context(stage);
  }
}

// conv 可为 nullptr（会话尚未建立时的早期失败路径）。
static void release_resources(std::vector<StageContext>& stages, Conversation* conv,
                              InputCbUserdata* input_cb_data,
                              embedding_info* embed_info, size_t embedding_size, Tokenizer* tokenizer)
{
  if (conv) {
    destroy_conversation(*conv);
  }
  destroy_contexts(stages);
  release_safetensors(input_cb_data);
  if (embed_info->embedding_data) {
    munmap(embed_info->embedding_data, embedding_size);
    embed_info->embedding_data = nullptr;
  }
  if (embed_info->fd != -1) {
    close(embed_info->fd);
    embed_info->fd = -1;
  }
  delete tokenizer;
}

static void reset_stage_slot(StageSlot& slot)
{
  std::lock_guard<std::mutex> lock(slot.mutex);
  slot.batches.clear();
  slot.expected_tokens = 0;
  slot.emitted_tokens = 0;
  slot.active_input_tokens = 0;
  slot.producer_done = false;
  slot.failed = false;
}

static void reset_pipeline(PipelineState& pipeline)
{
  for (auto& slot : pipeline.slots) {
    reset_stage_slot(*slot);
  }
}

static void close_stage_slot(StageSlot& slot)
{
  {
    std::lock_guard<std::mutex> lock(slot.mutex);
    slot.producer_done = true;
  }
  slot.cv.notify_all();
}

static void fail_pipeline(PipelineState& pipeline)
{
  for (auto& slot_ptr : pipeline.slots) {
    StageSlot& slot = *slot_ptr;
    {
      std::lock_guard<std::mutex> lock(slot.mutex);
      slot.failed = true;
    }
    slot.cv.notify_all();
  }
}

static bool pipeline_failed(PipelineState& pipeline)
{
  for (auto& slot_ptr : pipeline.slots) {
    std::lock_guard<std::mutex> lock(slot_ptr->mutex);
    if (slot_ptr->failed) {
      return true;
    }
  }
  return false;
}

// 结果槽挂在 Conversation 上（不再是全局），多会话并发时每个会话各读各的。
static void reset_last_stage_result(Conversation& conv)
{
  std::lock_guard<std::mutex> lock(conv.result.mutex);
  conv.result.has_token = false;
  conv.result.next_token = -1;
}

static bool get_last_stage_token(Conversation& conv, int32_t* token)
{
  std::lock_guard<std::mutex> lock(conv.result.mutex);
  if (!conv.result.has_token) {
    return false;
  }
  *token = conv.result.next_token;
  return true;
}

static bool parse_positive_u64(const char* value, uint64_t* result)
{
  if (!value || !result || value[0] == '\0' || value[0] == '-') {
    return false;
  }

  errno = 0;
  char* end = nullptr;
  unsigned long long parsed = strtoull(value, &end, 10);
  if (errno != 0 || end == value || *end != '\0' || parsed == 0) {
    return false;
  }

  *result = (uint64_t)parsed;
  return true;
}

static bool parse_positive_i32(const char* value, int32_t* result)
{
  uint64_t parsed = 0;
  if (!result || !parse_positive_u64(value, &parsed) || parsed > 0x7fffffffU) {
    return false;
  }
  *result = (int32_t)parsed;
  return true;
}

static bool parse_bool01(const char* value, bool* result)
{
  if (!value || !result) {
    return false;
  }
  if (strcmp(value, "0") == 0) {
    *result = false;
    return true;
  }
  if (strcmp(value, "1") == 0) {
    *result = true;
    return true;
  }
  return false;
}

static bool parse_core_mask(const char* value, uint32_t* result)
{
  if (!value || !result || value[0] == '\0' || value[0] == '-') {
    return false;
  }

  errno = 0;
  char* end = nullptr;
  unsigned long long parsed = strtoull(value, &end, 0);
  if (errno != 0 || end == value || *end != '\0') {
    errno = 0;
    end = nullptr;
    parsed = strtoull(value, &end, 16);
  }
  if (errno != 0 || end == value || *end != '\0' || parsed > 0xffffffffULL) {
    return false;
  }

  *result = (uint32_t)parsed;
  return true;
}

struct CommandLineOptions
{
  const char* stage0_model = nullptr;
  const char* stage0_weight = nullptr;
  const char* tokenizer = nullptr;
  const char* embedding = nullptr;
  int32_t     max_context_len = 0;
  uint32_t    run_core_mask = 0;
  size_t      stage_count = 0;
  uint64_t    bucket_size = 0;
  bool        has_max_context_len = false;
  bool        has_run_core_mask = false;
  bool        has_stage_count = false;
  bool        has_bucket_size = false;

  const char* prompt = nullptr;
  int         max_new_tokens = 512;
  bool        verbose = false;
  bool        ignore_eos = false;
  bool        interactive = false;
  // --serve：多会话服务模式。与 --interactive 共用同一套「谁空闲给谁」的驱动，只把
  // stdin/stdout 的编解码换成长度前缀的帧，并给每条回复带上请求 id。交互模式整段成块
  // 且只有 [s<i>] 前缀，网关无法把回复关联回请求，也就无法并发。
  bool        serve = false;
  // 帧通道的 fd（--serve-fd）。默认 3；网关用 pass_fds 传进来时按实际号覆盖它。
  int         serve_fd = 3;
  const char* rope_path = nullptr;
  const char* chat_template = nullptr;
  const char* tensor_dump_dir = nullptr;
  // --dump-tokens <path>：把每步采样出的 token id 逐行写入文件，用于回归对比
  // （改造前后必须逐 token 一致）以及 P2 多会话下 stdout 交错时的逐会话核对。
  const char* dump_tokens = nullptr;
  std::vector<std::string> device_ids;
  // --probe-sessions N：多 session 可行性探针（每卡再建 N-1 个 session 并测量设备内存）
  int         probe_sessions = 0;
  // --sessions N：并发驱动 N 个会话。**不指定**时走原来的单会话路径，
  // 保证与改造前逐字节一致；显式指定（含 N=1）才进入并发执行器。
  int         sessions = 0;
  bool        has_sessions = false;
  // --rounds M：并发模式下每个会话跑 M 轮（首轮用 --prompt，后续轮用 chat 模板追加）。
  int         rounds = 1;

  bool     performance_mode = false;
  uint64_t performance_input_length = 0;
  uint64_t performance_output_length = 0;
};

static void print_usage(const char* program)
{
  printf("Usage:\n");
  printf("  %s \\\n", program);
  printf("    --model <path> --weight <path> \\\n"
         "    --vocab <path> --embed <path> \\\n"
         "    -c, --ctx-size <tokens> --core-mask <mask> \\\n"
         "    --stage-count <count> --bucket-size <tokens> [options]\n");
  printf("Options:\n");
  printf("  --prompt <text-or-file>       prompt text or a .txt prompt file\n");
  printf("  --dump-tokens <path>          append every sampled token id (one per line)\n");
  printf("  -n/--predict/--n-predict <count>  maximum generated tokens (default: 512)\n");
  printf("  --verbose                     enable verbose logs\n");
  printf("  --ignore-eos                  ignore EOS during generation\n");
  printf("  --interactive, -i             interactive multi-turn chat mode (reads stdin)\n"
         "                                with --sessions N: each stdin line is dispatched to\n"
         "                                an idle session; replies print as [s<i>] blocks\n");
  printf("  --rope-tensor <safetensors>   external rope cache\n");
  printf("  --chat-template <auto|qwen|gemma>  multi-turn chat markers; auto (default)\n"
         "                                detects the model family from the rope cache\n");
  printf("  --device-id <id[#id...]>      device IDs separated by '#'; optional\n");
  printf("  --perf <input> <output>       performance test mode\n");
  printf("  --probe-sessions <N>          multi-session feasibility probe (N in [2,16]);\n"
         "                                creates N-1 extra sessions per card, measures device\n"
         "                                memory and verifies KV-cache isolation, then exits\n");
  printf("  --sessions <N>                run N conversations concurrently (N in [1,16]).\n"
         "                                Omit to keep the original single-session path.\n"
         "                                Same-card sessions are serialized by a per-card\n"
         "                                lock, so the shared output tensors stay safe;\n"
         "                                the 4 pipeline stages still run in parallel.\n");
  printf("  --rounds <M>                  with --sessions: M turns per conversation (default: 1);\n"
         "                                turn 1 uses --prompt, later turns continue the chat\n");
  printf("  --serve                       with --sessions N: backend protocol for the OpenAI\n"
         "                                gateway (needs the gateway for the HTTP side).\n"
         "                                Requests on stdin: 'REQ <rid> <session| -1>\n"
         "                                <max_new_tokens> <reset> <prompt_len>' + prompt_len\n"
         "                                raw bytes, one frame per request; 'QUIT' to stop.\n"
         "                                Replies: DELTA/DONE/CLEAR/ERR/REJECT frames on the\n"
         "                                frame fd (--serve-fd, default 3), so stray logs on\n"
         "                                stdout cannot corrupt the stream. ERR = that\n"
         "                                session's driver thread exited (the session is\n"
         "                                dead); REJECT = this turn was refused before it\n"
         "                                ran, the session is still usable.\n"
         "                                Also emits 'READY <nsessions> <default_n>' first.\n"
         "  --serve-fd <n>                frame channel fd for --serve (default: 3). The\n"
         "                                gateway passes its pipe write end and names it here\n");
  printf("  --dump-tensors <dir>          dump callback tensors to this directory\n");
  printf("  --help                        show this message\n");
  printf("Legacy positional arguments remain supported for compatibility.\n");
}

static bool take_option_value(int argc, char** argv, int* index,
                              const char* option, const char** value)
{
  if (!index || !value || *index + 1 >= argc || argv[*index + 1][0] == '\0' ||
      strncmp(argv[*index + 1], "--", 2) == 0) {
    printf("%s requires a value\n", option);
    return false;
  }
  *value = argv[++(*index)];
  return true;
}

static bool append_device_ids(const char* value, std::vector<std::string>* device_ids)
{
  if (!value || !device_ids || value[0] == '\0') {
    return false;
  }

  std::string all_ids = value;
  size_t start = 0;
  while (start <= all_ids.size()) {
    size_t end = all_ids.find('#', start);
    if (end == start) {
      return false;
    }
    device_ids->push_back(all_ids.substr(start, end == std::string::npos
                                                   ? std::string::npos
                                                   : end - start));
    if (end == std::string::npos) {
      break;
    }
    start = end + 1;
    if (start == all_ids.size()) {
      return false;
    }
  }
  return true;
}

static bool validate_command_line_options(const CommandLineOptions& options)
{
  if (!options.stage0_model || !options.stage0_weight || !options.tokenizer ||
      !options.embedding || !options.has_max_context_len ||
      !options.has_run_core_mask || !options.has_stage_count ||
      !options.has_bucket_size) {
    printf("missing required command-line option\n");
    return false;
  }
  if (options.max_context_len <= 0 || options.stage_count == 0 || options.bucket_size == 0) {
    printf("max-context-len, stage-count and bucket-size must be positive\n");
    return false;
  }
  if (!options.device_ids.empty() && options.device_ids.size() != options.stage_count) {
    printf("expected %zu --device-id values, got %zu\n",
           options.stage_count, options.device_ids.size());
    return false;
  }
  if (options.has_sessions) {
    // --perf 用它自己的长度驱动的循环，还没并进并发执行器；这里直接拒绝，
    // 免得出现「参数接受了但行为没测过」的组合。
    if (options.performance_mode) {
      printf("--sessions cannot be combined with --perf yet; "
             "use the default single-session path for --perf\n");
      return false;
    }
    if (options.probe_sessions > 0) {
      printf("--sessions cannot be combined with --probe-sessions\n");
      return false;
    }
    // --interactive 与 --rounds 是两种不同的驱动方式：前者由 stdin 决定轮数，
    // 后者由 --rounds 决定。同时给出来只会让人以为两个都生效。
    if (options.interactive && options.rounds > 1) {
      printf("--rounds does not apply to --interactive; each stdin line is one turn\n");
      return false;
    }
    // --interactive 与 --serve 是同一套驱动的两个前端，同时给出来只说明没想清楚要哪个。
    if (options.interactive && options.serve) {
      printf("--interactive and --serve are two front-ends of the same driver; pick one\n");
      return false;
    }
    if (options.serve && options.rounds > 1) {
      printf("--rounds does not apply to --serve; each request frame is one turn\n");
      return false;
    }
    if (!options.interactive && !options.serve && options.rounds > 1 && !options.prompt) {
      printf("--rounds requires --prompt as the first turn's input\n");
      return false;
    }
  } else if (options.serve) {
    // 会话数由 --sessions 决定；没有会话池，网关那套按会话粘住 KV 的复用就无从谈起。
    printf("--serve requires --sessions N (the gateway multiplexes N sessions)\n");
    return false;
  } else if (options.rounds > 1) {
    printf("--rounds only applies together with --sessions\n");
    return false;
  }
  return true;
}

static bool parse_named_command_line(int argc, char** argv, CommandLineOptions* options)
{
  for (int i = 1; i < argc; ++i) {
    const char* arg = argv[i];
    const char* value = nullptr;

    if (strcmp(arg, "-m") == 0 || strcmp(arg, "--model") == 0 ||
        strcmp(arg, "--stage0-model") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value)) return false;
      options->stage0_model = value;
    } else if (strcmp(arg, "--weight") == 0 || strcmp(arg, "--stage0-weight") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value)) return false;
      options->stage0_weight = value;
    } else if (strcmp(arg, "--vocab") == 0 || strcmp(arg, "--tokenizer") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value)) return false;
      options->tokenizer = value;
    } else if (strcmp(arg, "--embed") == 0 || strcmp(arg, "--embedding") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value)) return false;
      options->embedding = value;
    } else if (strcmp(arg, "-c") == 0 || strcmp(arg, "--ctx-size") == 0 ||
               strcmp(arg, "--max-context-len") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !parse_positive_i32(value, &options->max_context_len)) {
        printf("%s requires a positive integer\n", arg);
        return false;
      }
      options->has_max_context_len = true;
    } else if (strcmp(arg, "--core-mask") == 0 || strcmp(arg, "--run-core-mask") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !parse_core_mask(value, &options->run_core_mask)) {
        printf("%s requires a valid hexadecimal mask\n", arg);
        return false;
      }
      options->has_run_core_mask = true;
    } else if (strcmp(arg, "--stage-count") == 0) {
      uint64_t parsed = 0;
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !parse_positive_u64(value, &parsed) || parsed > (uint64_t)(size_t)-1) {
        printf("%s requires a positive integer\n", arg);
        return false;
      }
      options->stage_count = (size_t)parsed;
      options->has_stage_count = true;
    } else if (strcmp(arg, "--bucket-size") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !parse_positive_u64(value, &options->bucket_size)) {
        printf("%s requires a positive integer\n", arg);
        return false;
      }
      options->has_bucket_size = true;
    } else if (strcmp(arg, "--prompt") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &options->prompt)) return false;
    } else if (strcmp(arg, "-n") == 0 || strcmp(arg, "--predict") == 0 ||
               strcmp(arg, "--n-predict") == 0 || strcmp(arg, "--max-new-tokens") == 0) {
      int32_t parsed = 0;
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !parse_positive_i32(value, &parsed)) {
        printf("%s requires a positive integer\n", arg);
        return false;
      }
      options->max_new_tokens = (int)parsed;
    } else if (strcmp(arg, "--verbose") == 0) {
      options->verbose = true;
    } else if (strcmp(arg, "--no-verbose") == 0) {
      options->verbose = false;
    } else if (strcmp(arg, "--ignore-eos") == 0) {
      options->ignore_eos = true;
    } else if (strcmp(arg, "--no-ignore-eos") == 0) {
      options->ignore_eos = false;
    } else if (strcmp(arg, "--interactive") == 0 || strcmp(arg, "-i") == 0) {
      options->interactive = true;
    } else if (strcmp(arg, "--serve") == 0) {
      options->serve = true;
    } else if (strcmp(arg, "--serve-fd") == 0) {
      const char* fd_value = nullptr;
      uint64_t    fd_number = 0;
      if (!take_option_value(argc, argv, &i, arg, &fd_value) ||
          !parse_positive_u64(fd_value, &fd_number) || fd_number > 1023) {
        printf("%s requires a fd in [1, 1023] (got '%s')\n",
               arg, fd_value ? fd_value : "");
        return false;
      }
      options->serve_fd = (int)fd_number;
    } else if (strcmp(arg, "--rope-tensor") == 0 || strcmp(arg, "--rope") == 0 ||
               strcmp(arg, "--rope-path") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &options->rope_path)) return false;
    } else if (strcmp(arg, "--chat-template") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &options->chat_template)) return false;
      if (strcmp(options->chat_template, "auto") != 0 &&
          strcmp(options->chat_template, "qwen") != 0 &&
          strcmp(options->chat_template, "gemma") != 0) {
        printf("%s expects one of: auto, qwen, gemma (got '%s')\n",
               arg, options->chat_template);
        return false;
      }
    } else if (strcmp(arg, "--dump-tokens") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &options->dump_tokens)) return false;
    } else if (strcmp(arg, "--probe-sessions") == 0) {
      const char* probe_value = nullptr;
      uint64_t    probe_count = 0;
      if (!take_option_value(argc, argv, &i, arg, &probe_value) ||
          !parse_positive_u64(probe_value, &probe_count) ||
          probe_count < 2 || probe_count > 16) {
        printf("%s requires a session count in [2, 16] (got '%s')\n",
               arg, probe_value ? probe_value : "");
        return false;
      }
      options->probe_sessions = (int)probe_count;
    } else if (strcmp(arg, "--sessions") == 0) {
      const char* sessions_value = nullptr;
      uint64_t    session_count = 0;
      if (!take_option_value(argc, argv, &i, arg, &sessions_value) ||
          !parse_positive_u64(sessions_value, &session_count) ||
          session_count < 1 || session_count > 16) {
        printf("%s requires a session count in [1, 16] (got '%s')\n",
               arg, sessions_value ? sessions_value : "");
        return false;
      }
      options->sessions = (int)session_count;
      options->has_sessions = true;
    } else if (strcmp(arg, "--rounds") == 0) {
      const char* rounds_value = nullptr;
      uint64_t    round_count = 0;
      if (!take_option_value(argc, argv, &i, arg, &rounds_value) ||
          !parse_positive_u64(rounds_value, &round_count) || round_count > 1000) {
        printf("%s requires a round count in [1, 1000] (got '%s')\n",
               arg, rounds_value ? rounds_value : "");
        return false;
      }
      options->rounds = (int)round_count;
    } else if (strcmp(arg, "--device-id") == 0) {
      if (!take_option_value(argc, argv, &i, arg, &value) ||
          !append_device_ids(value, &options->device_ids)) {
        printf("%s requires one or more device IDs separated by '#': id0#id1#...\n", arg);
        return false;
      }
    } else if (strcmp(arg, "--perf") == 0 || strcmp(arg, "--performance") == 0) {
      const char* input_value = nullptr;
      const char* output_value = nullptr;
      if (options->performance_mode) {
        printf("performance mode can only be specified once\n");
        return false;
      }
      if (!take_option_value(argc, argv, &i, arg, &input_value) ||
          !take_option_value(argc, argv, &i, arg, &output_value) ||
          !parse_positive_u64(input_value, &options->performance_input_length) ||
          !parse_positive_u64(output_value, &options->performance_output_length)) {
        printf("%s requires positive <input_tokens> and <output_tokens>\n", arg);
        return false;
      }
      options->performance_mode = true;
    } else if (strcmp(arg, "--dump-tensors") == 0) {
      if (options->tensor_dump_dir ||
          !take_option_value(argc, argv, &i, arg, &options->tensor_dump_dir)) {
        printf("%s requires a value and can only be specified once\n", arg);
        return false;
      }
    } else if (strcmp(arg, "--help") == 0) {
      return false;
    } else {
      printf("unknown or positional argument in named mode: %s\n", arg);
      return false;
    }
  }

  return validate_command_line_options(*options);
}

static bool parse_legacy_command_line(int argc, char** argv, CommandLineOptions* options)
{
  if (argc < 9) {
    printf("legacy mode requires 8 positional arguments\n");
    return false;
  }

  options->stage0_model = argv[1];
  options->stage0_weight = argv[2];
  options->tokenizer = argv[3];
  options->embedding = argv[4];
  if (!parse_positive_i32(argv[5], &options->max_context_len) ||
      !parse_core_mask(argv[6], &options->run_core_mask)) {
    printf("invalid legacy max_context_len or run_core_mask\n");
    return false;
  }
  uint64_t parsed_stage_count = 0;
  if (!parse_positive_u64(argv[7], &parsed_stage_count) ||
      parsed_stage_count > (uint64_t)(size_t)-1 ||
      !parse_positive_u64(argv[8], &options->bucket_size)) {
    printf("invalid legacy stage_count or bucket_size\n");
    return false;
  }
  options->stage_count = (size_t)parsed_stage_count;
  options->has_max_context_len = true;
  options->has_run_core_mask = true;
  options->has_stage_count = true;
  options->has_bucket_size = true;

  std::vector<const char*> optional_args;
  for (int i = 9; i < argc; ++i) {
    const char* arg = argv[i];
    if (strcmp(arg, "--perf") == 0 || strcmp(arg, "--performance") == 0) {
      const char* input_value = nullptr;
      const char* output_value = nullptr;
      if (options->performance_mode) {
        printf("performance mode can only be specified once\n");
        return false;
      }
      if (!take_option_value(argc, argv, &i, arg, &input_value) ||
          !take_option_value(argc, argv, &i, arg, &output_value) ||
          !parse_positive_u64(input_value, &options->performance_input_length) ||
          !parse_positive_u64(output_value, &options->performance_output_length)) {
        printf("%s requires positive <input_tokens> and <output_tokens>\n", arg);
        return false;
      }
      options->performance_mode = true;
    } else if (strcmp(arg, "--dump-tensors") == 0) {
      if (options->tensor_dump_dir ||
          !take_option_value(argc, argv, &i, arg, &options->tensor_dump_dir)) {
        printf("%s requires a value and can only be specified once\n", arg);
        return false;
      }
    } else if (arg[0] == '-') {
      printf("unknown option in legacy mode: %s\n", arg);
      return false;
    } else {
      optional_args.push_back(arg);
    }
  }

  if (optional_args.size() > 5 + options->stage_count) {
    printf("too many legacy optional arguments\n");
    return false;
  }
  if (optional_args.size() >= 1) options->prompt = optional_args[0];
  if (optional_args.size() >= 2) {
    int32_t parsed = 0;
    if (!parse_positive_i32(optional_args[1], &parsed)) {
      printf("invalid legacy max_new_tokens: %s\n", optional_args[1]);
      return false;
    }
    options->max_new_tokens = (int)parsed;
  }
  if (optional_args.size() >= 3 && !parse_bool01(optional_args[2], &options->verbose)) {
    printf("legacy verbose must be 0 or 1\n");
    return false;
  }
  if (optional_args.size() >= 4 && !parse_bool01(optional_args[3], &options->ignore_eos)) {
    printf("legacy ignore_eos must be 0 or 1\n");
    return false;
  }
  if (optional_args.size() >= 5) options->rope_path = optional_args[4];
  for (size_t i = 5; i < optional_args.size(); ++i) {
    if (!append_device_ids(optional_args[i], &options->device_ids)) {
      printf("invalid legacy device ID list: %s\n", optional_args[i]);
      return false;
    }
  }

  return validate_command_line_options(*options);
}

static bool parse_command_line(int argc, char** argv, CommandLineOptions* options)
{
  if (!options || argc < 2) {
    return false;
  }
  if (argv[1][0] == '-') {
    return parse_named_command_line(argc, argv, options);
  }
  return parse_legacy_command_line(argc, argv, options);
}

static void build_performance_input_tokens(const VocabInfo& vocab_info, uint64_t input_length,
                                           std::vector<int32_t>* input_tokens)
{
  int32_t bos_token = -1;
  if (vocab_info.n_special_bos_id > 0 &&
      vocab_info.special_bos_id[0] >= 0 &&
      vocab_info.special_bos_id[0] < vocab_info.vocab_size) {
    bos_token = vocab_info.special_bos_id[0];
  }

  int32_t fill_token = vocab_info.linefeed_id;
  if (fill_token < 0 || fill_token >= vocab_info.vocab_size) {
    fill_token = bos_token >= 0 ? bos_token : 0;
  }
  input_tokens->assign((size_t)input_length, fill_token);
  if (bos_token >= 0 && !input_tokens->empty()) {
    input_tokens->front() = bos_token;
  }
}

static bool name_contains(const char* name, const char* needle)
{
  return name && needle && strstr(name, needle) != nullptr;
}

static void dump_tensor_blob(const TensorBlob& blob, size_t index, const char* prefix)
{
  VLOG("%s tensor[%zu]: name=%s, dtype=%d, n_elems=%u, aligned_size=%llu\n",
       prefix,
       index,
       blob.attr.name,
       (int)blob.attr.dtype,
       blob.attr.n_elems,
       (unsigned long long)blob.attr.aligned_size);
}

static bool configure_tensor_dump(const char* path)
{
  if (!path || path[0] == '\0') {
    return false;
  }

  struct stat st;
  if (stat(path, &st) == 0) {
    if (!S_ISDIR(st.st_mode)) {
      printf("tensor dump path is not a directory: %s\n", path);
      return false;
    }
  } else {
    if (errno != ENOENT || mkdir(path, 0755) != 0) {
      printf("failed to create tensor dump directory: %s, error=%s\n",
             path, strerror(errno));
      return false;
    }
  }

  g_tensor_dump_dir = path;
  g_tensor_dump_enabled = true;
  printf("tensor dump enabled: %s\n", g_tensor_dump_dir.c_str());
  return true;
}

static std::string sanitize_tensor_dump_name(const char* name)
{
  std::string result = name ? name : "unnamed";
  for (size_t i = 0; i < result.size(); ++i) {
    unsigned char c = (unsigned char)result[i];
    if (!isalnum(c) && c != '_' && c != '-' && c != '.') {
      result[i] = '_';
    }
  }
  if (result.empty()) {
    result = "unnamed";
  }
  return result;
}

static std::string make_tensor_dump_base_path(const char* callback_name,
                                              uint64_t call_index,
                                              uint32_t tensor_index,
                                              const char* tensor_name)
{
  char prefix[128];
  snprintf(prefix, sizeof(prefix), "%s_%08llu_tensor_%03u_",
           callback_name,
           (unsigned long long)call_index,
           tensor_index);

  std::string path = g_tensor_dump_dir;
  if (!path.empty() && path.back() != '/') {
    path.push_back('/');
  }
  path += prefix;
  path += sanitize_tensor_dump_name(tensor_name);
  return path;
}

// The caller holds g_tensor_dump_mutex while this function runs.
static void dump_tensor_data_locked(const char* callback_name,
                                    uint64_t call_index,
                                    uint32_t tensor_index,
                                    const char* tensor_name,
                                    const void* data,
                                    size_t data_size,
                                    const rknn3_tensor_attr* attr,
                                    const char* extra_metadata)
{
  std::string base_path = make_tensor_dump_base_path(callback_name, call_index,
                                                     tensor_index, tensor_name);
  std::string data_path = base_path + ".bin";
  FILE* data_file = fopen(data_path.c_str(), "wb");
  if (!data_file) {
    printf("failed to open tensor dump file: %s, error=%s\n",
           data_path.c_str(), strerror(errno));
    return;
  }

  bool write_ok = true;
  if (data_size > 0 && (!data || fwrite(data, 1, data_size, data_file) != data_size)) {
    write_ok = false;
  }
  if (fclose(data_file) != 0) {
    write_ok = false;
  }
  if (!write_ok) {
    printf("failed to write tensor dump file: %s\n", data_path.c_str());
    return;
  }

  std::string metadata_path = base_path + ".txt";
  FILE* metadata_file = fopen(metadata_path.c_str(), "w");
  if (!metadata_file) {
    printf("failed to open tensor dump metadata: %s, error=%s\n",
           metadata_path.c_str(), strerror(errno));
    return;
  }

  fprintf(metadata_file, "callback=%s\n", callback_name);
  fprintf(metadata_file, "call_index=%llu\n", (unsigned long long)call_index);
  fprintf(metadata_file, "tensor_index=%u\n", tensor_index);
  fprintf(metadata_file, "name=%s\n", tensor_name ? tensor_name : "");
  fprintf(metadata_file, "bytes=%zu\n", data_size);
  fprintf(metadata_file, "data_file=%s\n", data_path.c_str());
  if (attr) {
    fprintf(metadata_file, "attr_index=%u\n", attr->index);
    fprintf(metadata_file, "dtype=%d\n", (int)attr->dtype);
    fprintf(metadata_file, "layout=%d\n", (int)attr->layout);
    fprintf(metadata_file, "n_dims=%u\n", attr->n_dims);
    fprintf(metadata_file, "shape=");
    for (uint32_t i = 0; i < attr->n_dims; ++i) {
      fprintf(metadata_file, "%s%u", i == 0 ? "" : ",", attr->shape[i]);
    }
    fprintf(metadata_file, "\n");
    fprintf(metadata_file, "n_elems=%u\n", attr->n_elems);
    fprintf(metadata_file, "aligned_size=%llu\n",
            (unsigned long long)attr->aligned_size);
    fprintf(metadata_file, "n_stride=%u\n", attr->n_stride);
    fprintf(metadata_file, "stride=");
    for (uint32_t i = 0; i < attr->n_stride; ++i) {
      fprintf(metadata_file, "%s%llu", i == 0 ? "" : ",",
              (unsigned long long)attr->stride[i]);
    }
    fprintf(metadata_file, "\n");
  }
  if (extra_metadata && extra_metadata[0] != '\0') {
    fprintf(metadata_file, "%s\n", extra_metadata);
  }
  fclose(metadata_file);

  VLOG("[tensor_dump] %s\n", data_path.c_str());
}

static void dump_embed_callback_tensor(const void* embed, uint64_t num_tokens,
                                       uint64_t len, int embedding_dim)
{
  if (!g_tensor_dump_enabled) {
    return;
  }

  std::lock_guard<std::mutex> lock(g_tensor_dump_mutex);
  uint64_t call_index = g_embed_dump_count++;
  char extra_metadata[256];
  snprintf(extra_metadata, sizeof(extra_metadata),
           "dtype=1\nshape=%llu,%d\nnum_tokens=%llu\nembedding_dim=%d",
           (unsigned long long)num_tokens,
           embedding_dim,
           (unsigned long long)num_tokens,
           embedding_dim);
  dump_tensor_data_locked("embed_callback", call_index, 0, "embedding",
                          embed, (size_t)len, nullptr, extra_metadata);
}

static void dump_input_callback_tensors(rknn3_tensor* input_tensors,
                                         uint32_t n_input_tensors,
                                         LLMInputCallbackParam param)
{
  if (!g_tensor_dump_enabled) {
    return;
  }

  std::lock_guard<std::mutex> lock(g_tensor_dump_mutex);
  uint64_t call_index = g_input_dump_count++;
  for (uint32_t i = 0; i < n_input_tensors; ++i) {
    const rknn3_tensor_attr* attr = input_tensors[i].attr;
    const rknn3_tensor_mem* mem = input_tensors[i].mem;
    char fallback_name[32];
    snprintf(fallback_name, sizeof(fallback_name), "tensor_%u", i);
    const char* tensor_name = attr ? attr->name : fallback_name;

    size_t data_size = attr ? (size_t)attr->aligned_size : 0;
    if (mem && mem->size > 0 && (data_size == 0 || mem->size < data_size)) {
      data_size = (size_t)mem->size;
    }

    char extra_metadata[256];
    snprintf(extra_metadata, sizeof(extra_metadata),
             "pos=%d\nshape_id=%d\nnum_tokens=%d\nmrope_pos=%d\n"
             "mrope_start=%d\nsystem_prompt_seqlen=%d\n"
             "valid_system_prompt_seqlen=%d\nmax_position_embeddings=%d",
             param.pos,
             param.shape_id,
             param.num_tokens,
             param.mrope_pos,
             param.mrope_start,
             param.system_prompt_seqlen,
             param.valid_system_prompt_seqlen,
             param.max_position_embeddings);
    dump_tensor_data_locked("input_callback", call_index, i, tensor_name,
                            mem ? mem->virt_addr : nullptr, data_size,
                            attr, extra_metadata);
  }
}

static const TensorBlob* pick_embed_tensor(const std::vector<TensorBlob>& tensors)
{
  if (tensors.empty()) {
    return nullptr;
  }

  for (const auto& tensor : tensors) {
    if (name_contains(tensor.attr.name, "hidden") || name_contains(tensor.attr.name, "last_hidden") ||
        name_contains(tensor.attr.name, "output")) {
      return &tensor;
    }
  }

  return &tensors.front();
}

static int tokenizer_callback(void* userdata, const char* text, int32_t text_len, int32_t* tokens, int32_t n_tokens_max)
{
  Tokenizer* tokenizer = (Tokenizer*)userdata;
  if (!tokenizer || !text || !tokens || n_tokens_max <= 0) {
    return -1;
  }

  // 这里**必须**取 g_tokenizer_mutex，理由是那把锁自己的注释里写的那条：
  // Tokenizer 没有线程安全承诺，所以 decode 路径（result_callback -> TokenToPiece/
  // Decode）一直串行化着。但 prefill 路径的 Tokenize 是从**本回调**进的，而它跑在
  // 另一张卡的那个会话线程上——两边各自持有的是**不同的 run_mutex**，那把锁根本
  // 排除不了彼此。于是 card 0 上 prefill 的 Tokenize 会和 card 3 上 decode 的 Decode
  // 同时进同一个 tokenizer 对象。成本可忽略（一次微秒级，相比 80ms/token）。
  // 注：TSan 看不到这条——tokenizer 在闭源的预编译库里，没被插桩。
  int n_tokens = 0;
  {
    std::lock_guard<std::mutex> tokenizer_lock(g_tokenizer_mutex);
    n_tokens = tokenizer->Tokenize(text, text_len, tokens, n_tokens_max);
  }
  VLOG("[tokenizer_callback] text=%s, text_len=%d, n_tokens=%d\n", text, text_len, n_tokens);
  if (n_tokens <= 0) {
    printf("tokenizer failed for input text\n");
  }
  return n_tokens;
}

static int embed_callback(void* userdata, int32_t* tokens, uint64_t num_tokens, void* embed, uint64_t len)
{
  EmbedCallbackContext* ctx = (EmbedCallbackContext*)userdata;
  embedding_info* info = ctx ? ctx->embed_info : nullptr;
  if (!info || !tokens || !embed || info->embedding_dim <= 0 || !info->embedding_data) {
    return -1;
  }

  if (len != num_tokens * (uint64_t)info->embedding_dim * sizeof(float16)) {
    printf("invalid embed buffer size\n");
    return -1;
  }

  for (uint64_t n = 0; n < num_tokens; ++n) {
    memcpy((unsigned char*)embed + n * info->embedding_dim * sizeof(float16),
           info->embedding_data + tokens[n] * info->embedding_dim,
           info->embedding_dim * sizeof(float16));
  }

  dump_embed_callback_tensor(embed, num_tokens, len, info->embedding_dim);

  // 统计 stage0 输入 token 数，用于 prefill 性能统计。
  if (ctx->pipeline && !ctx->pipeline->slots.empty()) {
    StageSlot& slot = *ctx->pipeline->slots.front();
    std::lock_guard<std::mutex> lock(slot.mutex);
    slot.expected_tokens += num_tokens;
    VLOG("[embed_callback] num_tokens=%llu, total=%llu, token_id=%d\n",
    (unsigned long long)num_tokens, (unsigned long long)slot.expected_tokens, tokens[num_tokens - 1]);
  }

  return 0;
}

static int result_callback(void* userdata, RKLLMResult* result, LLMCallState state)
{
  LastStageResultState* result_state = (LastStageResultState*)userdata;
  Tokenizer* tokenizer = result_state ? result_state->tokenizer : nullptr;

  if (state == RKLLM_RUN_NORMAL && result && tokenizer) {
    int32_t next_token = -1;
    if (result->num_tokens > 0) {
      next_token = result->token_ids[result->num_tokens - 1];
      std::lock_guard<std::mutex> lock(result_state->mutex);
      result_state->next_token = next_token;
      result_state->has_token = true;
    }

    // 并发模式下每个会话写自己的文件；单会话路径回落到全局文件，输出逐字节不变。
    FILE* token_dump = result_state->token_dump ? result_state->token_dump : g_token_dump;
    if (token_dump) {
      for (int i = 0; i < result->num_tokens; ++i) {
        fprintf(token_dump, "%d\n", result->token_ids[i]);
      }
      fflush(token_dump);
    }

    // 交互/服务模式：把这一轮生成的文本交给会话自己的落点（攒成块，或直接发帧）。
    // 放在静音判断之前——这两条路径正是靠它取代逐 token 的 stdout 流。
    if (result_state->out) {
      std::string piece;
      {
        std::lock_guard<std::mutex> tokenizer_lock(g_tokenizer_mutex);
        if (result->num_tokens == 1) {
          piece = tokenizer->TokenToPiece(result->token_ids[0]);
        } else {
          piece = tokenizer->Decode(result->token_ids, result->num_tokens);
        }
      }
      result_state->out->on_piece(piece);
      return 0;
    }

    if (g_performance_mode || g_suppress_generation_output) {
      return 0;
    }

    std::string piece;
    {
      std::lock_guard<std::mutex> tokenizer_lock(g_tokenizer_mutex);
      if (result->num_tokens == 1) {
        piece = tokenizer->TokenToPiece(result->token_ids[0]);
      } else {
        piece = tokenizer->Decode(result->token_ids, result->num_tokens);
      }
    }
    VLOG("[result_callback] %s, next_token=%d\n", piece.c_str(), next_token);
    printf("%s", piece.c_str());
    fflush(stdout);
  }

  return 0;
}

static int stage_output_callback(void* userdata, rknn3_tensor* output_tensors, uint32_t n_output_tensors, LLMOutputCallbackState state)
{
  auto* cb_ctx = reinterpret_cast<StageCallbackContext*>(userdata);
  if (!cb_ctx || !cb_ctx->pipeline || cb_ctx->stage_index >= cb_ctx->pipeline->slots.size()) {
    return -1;
  }

  StageSlot& slot = *cb_ctx->pipeline->slots[cb_ctx->stage_index];

  VLOG("[Stage %zu] output_callback: state=%d, n_outputs=%u\n", cb_ctx->stage_index, state, n_output_tensors);

  StageBatch batch;
  batch.tensors.reserve(n_output_tensors);

  for (uint32_t i = 0; i < n_output_tensors; ++i) {
    if (!output_tensors[i].attr || !output_tensors[i].mem || !output_tensors[i].mem->virt_addr) {
      continue;
    }

    TensorBlob blob;
    blob.attr = *output_tensors[i].attr;
    blob.data.resize((size_t)blob.attr.aligned_size);
    memcpy(blob.data.data(), output_tensors[i].mem->virt_addr, blob.data.size());
    dump_tensor_blob(blob, i, "  [captured]");
    batch.tensors.push_back(std::move(blob));
  }

  if (!batch.tensors.empty()) {
    {
      std::lock_guard<std::mutex> lock(slot.mutex);
      uint64_t remaining_tokens = slot.expected_tokens > slot.emitted_tokens
                                      ? slot.expected_tokens - slot.emitted_tokens
                                      : 0;
      if (slot.active_input_tokens > 0) {
        batch.n_tokens = slot.active_input_tokens;
      } else if (state == RKLLM_OUTPUT_CALLBACK_PREFILL_FINISHED) {
        batch.n_tokens = remaining_tokens;
      } else {
        // 运行时给 prefill 分块用的是**固定大小**（模型的 max dynamic seq len，这里是
        // 128），**不看 `--bucket-size`**。所以分块大小要从 output tensor 的元素数
        // 反推——否则 `--bucket-size` 一旦不是 128，记账就与真实分块脱节，现场报的是
        // "embed buffer too small"。取 min(chunk, remaining) 让单 token 的 decode 也
        // 走同一条路。
        //
        // 这里从"只在 n_tokens == 0 时才反推"改成"每次都反推"，是因为那条兜底路径
        // 在 decode 下根本进不去（remaining 通常为 1，min 出来是 1 不是 0），真正需要
        // 它的场合是 prefill 分块——而那正是原来算错的地方。代价是每 token 多一次
        // `pick_embed_tensor`（对 1~3 个 tensor 做几次子串查找），相对一次前向可以忽略。
        uint64_t chunk = g_bucket_size;
        const TensorBlob* embed = pick_embed_tensor(batch.tensors);
        if (embed && embed->attr.n_elems > 0 && cb_ctx->embedding_dim > 0) {
          chunk = embed->attr.n_elems / (uint64_t)cb_ctx->embedding_dim;
        }
        batch.n_tokens = remaining_tokens > chunk ? chunk : remaining_tokens;
      }
      slot.emitted_tokens += batch.n_tokens;
      slot.batches.push_back(std::move(batch));
    }
    slot.cv.notify_one();
  }

  return 0;
}

static int input_callback(void* userdata, rknn3_tensor* input_tensors, uint32_t n_input_tensors,
                          LLMInputCallbackParam param)
{
  InputCbUserdata* cb_data = (InputCbUserdata*)userdata;

  for (uint32_t i = 0; i < n_input_tensors; ++i) {
    int cache_index = find_rope_cache_index(cb_data->rope_format, input_tensors[i].attr->name);
    if (cache_index < 0) {
      continue;
    }

    const rope_cache_tensor* cache      = &cb_data->rope_caches[cache_index];
    const size_t             elem_sz    = get_dtype_elem_size(cache->dtype);
    const int                C1         = cache->shape[1];
    const size_t             c2_bytes   = (size_t)cache->shape[4] * elem_sz;
    const size_t             src_stride = (size_t)cache->shape[3] * c2_bytes;
    const size_t             dst_stride = (size_t)input_tensors[i].attr->shape[3] * c2_bytes;
    // 取 min 防止 src 越界读取
    const size_t             copy_stride = src_stride < dst_stride ? src_stride : dst_stride;
    const uint8_t*           src = (const uint8_t*)cache->data + (size_t)param.pos * c2_bytes;
    uint8_t*                 dst = (uint8_t*)input_tensors[i].mem->virt_addr;
    for (int c1 = 0; c1 < C1; c1++, src += src_stride, dst += dst_stride) {
      memcpy(dst, src, copy_stride);
    }
  }

  // Dump after the callback has filled the rope/input buffers.
  dump_input_callback_tensors(input_tensors, n_input_tensors, param);

  return 0;
}

static int load_safetensors(const char* path, rope_cache_tensor caches[4],
                            RopeCacheFormat* format_out,
                            int* fd_out, void** mmap_base_out, size_t* mmap_size_out)
{
  int         fd          = -1;
  void*       map         = MAP_FAILED;
  uint64_t    header_size = 0;
  struct stat st;
  int         ret         = -1;

  *format_out = RopeCacheFormat::NONE;

  fd = open(path, O_RDONLY);
  if (fd < 0) {
    printf("Failed to open safetensors file: %s\n", path);
    goto err;
  }
  if (fstat(fd, &st) < 0) {
    printf("Failed to stat safetensors file: %s\n", path);
    goto err;
  }

  if (read(fd, &header_size, 8) != 8) {
    printf("Failed to read safetensors header size\n");
    goto err;
  }
  if (header_size == 0 || header_size > (uint64_t)st.st_size - 8) {
    printf("Invalid safetensors header size: %" PRIu64 "\n", header_size);
    goto err;
  }

  map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
  if (map == MAP_FAILED) {
    printf("Failed to mmap safetensors file: %s\n", path);
    goto err;
  }

  {
    const char*    json_ptr  = (const char*)map + 8;
    const uint8_t* data_base = (const uint8_t*)map + 8 + header_size;
    try {
      nlohmann::json j = nlohmann::json::parse(json_ptr, json_ptr + header_size);

      nlohmann::json meta_index = nlohmann::json::parse(
          j.at("__metadata__").at("index").get<std::string>());

      const char* const* rope_names = nullptr;
      int                n_rope_caches = 0;
      RopeCacheFormat    rope_format = RopeCacheFormat::NONE;
      auto has_rope_tensor = [&meta_index, &j](const char* name) {
        return meta_index.find(name) != meta_index.end() && j.find(name) != j.end();
      };

      bool has_gemma4_names = true;
      for (int i = 0; i < 4; ++i) {
        if (!has_rope_tensor(GEMMA4_ROPE_CACHE_NAMES[i])) {
          has_gemma4_names = false;
          break;
        }
      }
      bool has_qwen35_names = true;
      for (int i = 0; i < 2; ++i) {
        if (!has_rope_tensor(QWEN35_ROPE_CACHE_NAMES[i])) {
          has_qwen35_names = false;
          break;
        }
      }

      if (has_gemma4_names) {
        rope_names = GEMMA4_ROPE_CACHE_NAMES;
        n_rope_caches = 4;
        rope_format = RopeCacheFormat::GEMMA4;
      } else if (has_qwen35_names) {
        rope_names = QWEN35_ROPE_CACHE_NAMES;
        n_rope_caches = 2;
        rope_format = RopeCacheFormat::QWEN35;
      } else {
        printf("safetensors must contain either Gemma4 rope caches ");
        printf("(rope_cos_cache_0/1, rope_sin_cache_0/1) or Qwen3.5 ");
        printf("rope caches (rope_cos_cache, rope_sin_cache)\n");
        ret = -1;
      }

      ret = rope_format == RopeCacheFormat::NONE ? -1 : 0;
      for (int i = 0; i < n_rope_caches && ret == 0; i++) {
        const auto& meta_t = meta_index.at(rope_names[i]);
        int         dtype  = meta_t.at("dtype").get<int>();
        int         layout = meta_t.at("layout").get<int>();

        const auto& t      = j.at(rope_names[i]);
        auto shape_v   = t.at("shape").get<std::vector<int>>();
        auto offsets_v = t.at("data_offsets").get<std::vector<int64_t>>();

        int n_dims = (int)shape_v.size();
        if (n_dims != 5 || layout != 3) {
          printf("Tensor '%s': expected 5-D NC1HWC2 (layout=%d, n_dims=%d)\n",
                 rope_names[i], layout, n_dims);
          ret = -1;
          break;
        }
        caches[i].data   = (void*)(data_base + offsets_v[0]);
        caches[i].n_dims = n_dims;
        caches[i].dtype  = dtype;
        caches[i].layout = layout;
        for (int d = 0; d < n_dims; d++) caches[i].shape[d] = shape_v[d];
        printf("Loaded %-24s  dtype=%-2d  shape=[%d,%d,%d,%d,%d]\n",
               rope_names[i], dtype,
               caches[i].shape[0], caches[i].shape[1], caches[i].shape[2],
               caches[i].shape[3], caches[i].shape[4]);
      }
      if (ret == 0) {
        *format_out = rope_format;
      }
    } catch (const nlohmann::json::exception& e) {
      printf("Failed to parse safetensors JSON: %s\n", e.what());
      ret = -1;
    }
  }

err:
  if (ret != 0) {
    if (map != MAP_FAILED) munmap(map, (size_t)st.st_size);
    if (fd >= 0) close(fd);
    return ret;
  }
  *fd_out        = fd;
  *mmap_base_out = map;
  *mmap_size_out = (size_t)st.st_size;
  return 0;
}

static int init_tokenizer_and_embedding(const char* tokenizer_path, const char* embedding_path, VocabInfo* vocab_info,
                                        Tokenizer** tokenizer, embedding_info* embed_info, struct stat* emb_st)
{
  *tokenizer = new Tokenizer(TOKENIZER_BACKEND_LLAMA, tokenizer_path);
  (*tokenizer)->GetVocabInfo(vocab_info);

  embed_info->fd = open(embedding_path, O_RDONLY);
  if (embed_info->fd == -1) {
    printf("Failed to open embedding file: %s\n", embedding_path);
    delete *tokenizer;
    *tokenizer = nullptr;
    return -1;
  }

  if (fstat(embed_info->fd, emb_st) == -1) {
    printf("Failed to get embedding file size\n");
    close(embed_info->fd);
    embed_info->fd = -1;
    delete *tokenizer;
    *tokenizer = nullptr;
    return -1;
  }

  embed_info->embedding_data = (float16*)mmap(NULL, emb_st->st_size, PROT_READ, MAP_PRIVATE, embed_info->fd, 0);
  if (embed_info->embedding_data == MAP_FAILED) {
    printf("Failed to mmap embedding file\n");
    embed_info->embedding_data = nullptr;
    close(embed_info->fd);
    embed_info->fd = -1;
    delete *tokenizer;
    *tokenizer = nullptr;
    return -1;
  }

  embed_info->vocab_size = vocab_info->vocab_size;
  embed_info->embedding_dim = (emb_st->st_size / vocab_info->vocab_size) / sizeof(float16);
  return 0;
}

static int init_output_tensors(StageContext& stage)
{
  rknn3_input_output_num io_num;
  memset(&io_num, 0, sizeof(io_num));
  int ret = rknn3_query(stage.ctx, RKNN3_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] query io num failed, ret=%d\n", stage.name.c_str(), ret);
    return -1;
  }

  stage.n_output_tensors = (int)io_num.n_output;
  stage.output_tensors = (rknn3_tensor*)calloc(io_num.n_output, sizeof(rknn3_tensor));
  if (!stage.output_tensors) {
    return -1;
  }

  for (uint32_t i = 0; i < io_num.n_output; ++i) {
    stage.output_tensors[i].attr = (rknn3_tensor_attr*)malloc(sizeof(rknn3_tensor_attr));
    if (!stage.output_tensors[i].attr) {
      // 释放前序已分配的 attr 和 output_tensors
      for (uint32_t j = 0; j < i; ++j) {
        if (stage.output_tensors[j].mem) {
          rknn3_destroy_mem(stage.ctx, stage.output_tensors[j].mem);
          stage.output_tensors[j].mem = nullptr;
        }
        free(stage.output_tensors[j].attr);
        stage.output_tensors[j].attr = nullptr;
      }
      free(stage.output_tensors);
      stage.output_tensors = nullptr;
      stage.n_output_tensors = 0;
      return -1;
    }
    memset(stage.output_tensors[i].attr, 0, sizeof(rknn3_tensor_attr));
    stage.output_tensors[i].attr->index = i;

    ret = rknn3_query(stage.ctx, RKNN3_QUERY_OUTPUT_ATTR, stage.output_tensors[i].attr, sizeof(rknn3_tensor_attr));
    if (ret != RKNN3_SUCCESS) {
      printf("[%s] query output attr[%u] failed, ret=%d\n", stage.name.c_str(), i, ret);
      // 释放前序已分配的资源
      for (uint32_t j = 0; j < i; ++j) {
        if (stage.output_tensors[j].mem) {
          rknn3_destroy_mem(stage.ctx, stage.output_tensors[j].mem);
          stage.output_tensors[j].mem = nullptr;
        }
        free(stage.output_tensors[j].attr);
        stage.output_tensors[j].attr = nullptr;
      }
      free(stage.output_tensors[i].attr);
      stage.output_tensors[i].attr = nullptr;
      free(stage.output_tensors);
      stage.output_tensors = nullptr;
      stage.n_output_tensors = 0;
      return -1;
    }

    stage.output_tensors[i].mem = rknn3_create_mem(stage.ctx,
                                                    stage.output_tensors[i].attr->aligned_size,
                                                    stage.output_tensors[i].attr->core_id,
                                                    RKNN3_FLAG_MEMORY_CACHEABLE);
    if (!stage.output_tensors[i].mem) {
      printf("[%s] create output mem[%u] failed\n", stage.name.c_str(), i);
      // 释放前序已分配的资源
      for (uint32_t j = 0; j < i; ++j) {
        if (stage.output_tensors[j].mem) {
          rknn3_destroy_mem(stage.ctx, stage.output_tensors[j].mem);
          stage.output_tensors[j].mem = nullptr;
        }
        free(stage.output_tensors[j].attr);
        stage.output_tensors[j].attr = nullptr;
      }
      free(stage.output_tensors[i].attr);
      stage.output_tensors[i].attr = nullptr;
      free(stage.output_tensors);
      stage.output_tensors = nullptr;
      stage.n_output_tensors = 0;
      return -1;
    }
  }

  return 0;
}

static int query_ext_input_indices(StageContext& stage)
{
  rknn3_input_output_num io_num;
  memset(&io_num, 0, sizeof(io_num));
  int ret = rknn3_query(stage.ctx, RKNN3_QUERY_IN_OUT_NUM, &io_num, sizeof(io_num));
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] query io num for ext inputs failed, ret=%d\n", stage.name.c_str(), ret);
    return -1;
  }

  int n_ext = 0;
  for (uint32_t i = 0; i < io_num.n_input; ++i) {
    rknn3_tensor_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.index = i;
    ret = rknn3_query(stage.ctx, RKNN3_QUERY_INPUT_ATTR, &attr, sizeof(attr));
    if (ret != RKNN3_SUCCESS) {
      printf("[%s] query input attr[%u] failed, ret=%d\n", stage.name.c_str(), i, ret);
      return -1;
    }
    if (strcmp(attr.name, "per_layer_inputs") == 0) {
      n_ext++;
    } else if (strstr(attr.name, "rope_cos_cache") || strstr(attr.name, "rope_sin_cache")) {
      n_ext++;
    }
  }

  if (n_ext == 0) {
    return 0;
  }

  stage.ext_input_indices = (int*)malloc(n_ext * sizeof(int));
  if (!stage.ext_input_indices) {
    printf("[%s] malloc ext_input_indices failed\n", stage.name.c_str());
    return -1;
  }
  stage.n_ext_inputs = 0;

  for (uint32_t i = 0; i < io_num.n_input; ++i) {
    rknn3_tensor_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.index = i;
    ret = rknn3_query(stage.ctx, RKNN3_QUERY_INPUT_ATTR, &attr, sizeof(attr));
    if (ret != RKNN3_SUCCESS) {
      return -1;
    }
    if (strcmp(attr.name, "per_layer_inputs") == 0) {
      stage.ext_input_indices[stage.n_ext_inputs++] = (int)i;
    } else if (strstr(attr.name, "rope_cos_cache") || strstr(attr.name, "rope_sin_cache")) {
      stage.ext_input_indices[stage.n_ext_inputs++] = (int)i;
    }
  }

  printf("[%s] found %d ext input tensors (per_layer_inputs/rope_caches)\n", stage.name.c_str(), stage.n_ext_inputs);
  return 0;
}

// 建立一张卡上与会话无关的资源：context、权重、模型初始化、输出张量。
// 多会话并发时这一步只做一次。
static bool init_stage_context(StageContext& stage, size_t stage_idx, const char* device_id,
                               const char* model_path, const char* weight_path,
                               uint32_t run_core_mask, InputCbUserdata* input_cb_data)
{
  stage.model_path = model_path;
  stage.weight_path = weight_path;
  stage.device_id = device_id ? device_id : "";

  printf("[%s] init stage: model=%s, weight=%s, device_id=%s\n",
         stage.name.c_str(), model_path, weight_path, device_id);

  rknn3_init_extend ext;
  memset(&ext, 0, sizeof(ext));
  ext.device_id = const_cast<char*>(device_id);

  int ret = rknn3_init(&stage.ctx, &ext);
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] rknn3_init failed, ret=%d\n", stage.name.c_str(), ret);
    return false;
  }

  ret = rknn3_load_model_from_path(stage.ctx, stage.model_path.c_str(), stage.weight_path.c_str());
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] rknn3_load_model_from_path failed, ret=%d\n", stage.name.c_str(), ret);
    destroy_context(stage);
    return false;
  }

  rknn3_config cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.run_core_mask = run_core_mask;
  ret = rknn3_model_init(stage.ctx, &cfg);
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] rknn3_model_init failed, ret=%d\n", stage.name.c_str(), ret);
    destroy_context(stage);
    return false;
  }
  // rknn3_set_input_name_alias(stage.ctx, "inputs_embeds", "input_embeds");
  // rknn3_set_input_name_alias(stage.ctx, "hidden_states", "input_embeds");
  rknn3_llm_config llm_cfg;
  memset(&llm_cfg, 0, sizeof(llm_cfg));
  ret = rknn3_query(stage.ctx, RKNN3_QUERY_LLM_CONFIG, &llm_cfg, sizeof(llm_cfg));
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] query llm config failed, ret=%d\n", stage.name.c_str(), ret);
    destroy_context(stage);
    return false;
  }
  stage.embedding_dim = (int32_t)llm_cfg.embedding_dim;
  stage.vocab_size = (int32_t)llm_cfg.vocab_size;
  stage.max_ctx_len = (int32_t)llm_cfg.max_ctx_len;

  if (init_output_tensors(stage) != 0) {
    printf("[%s] init output tensors failed\n", stage.name.c_str());
    destroy_context(stage);
    return false;
  }

  // 外部 rope cache 的 ext input 索引也是卡级信息，一次查好给所有会话共用。
  if (input_cb_data && input_cb_data->rope_mmap_base) {
    if (query_ext_input_indices(stage) != 0) {
      printf("[%s] query_ext_input_indices failed\n", stage.name.c_str());
      destroy_context(stage);
      return false;
    }
  }

  printf("[%s] context init done: embedding_dim=%d vocab_size=%d max_ctx_len=%d outputs=%d\n",
         stage.name.c_str(), stage.embedding_dim, stage.vocab_size, stage.max_ctx_len, stage.n_output_tensors);
  return true;
}

// 在一个已初始化的 StageContext 上建一个会话：session + 指向**本会话**的回调。
// 同一个 ctx 可以建多个 session（P0 已实测每卡上限 5 个）。
static bool init_conversation(const std::vector<StageContext>& stages, Conversation& conv,
                              size_t stage_idx, const rknn3_llm_param& session_param,
                              Tokenizer* tokenizer, embedding_info* embed_info,
                              InputCbUserdata* input_cb_data)
{
  const StageContext& stage = stages[stage_idx];
  StageSession& sess = conv.stages[stage_idx];
  const bool is_last_stage = (stage_idx + 1 == stages.size());

  sess.callback_ctx.pipeline = &conv.pipeline;
  sess.callback_ctx.stage_index = stage_idx;
  sess.callback_ctx.embedding_dim = stage.embedding_dim;
  sess.embed_ctx.embed_info = embed_info;
  sess.embed_ctx.pipeline = &conv.pipeline;

  rknn3_llm_param local_param = session_param;
  sess.session = rknn3_session_init(stage.ctx, &local_param, 1);
  if (!sess.session) {
    printf("[%s] rknn3_session_init failed\n", stage.name.c_str());
    return false;
  }
  rknn3_session_set_chat_template(sess.session, "", "", "");

  RKLLMCallback& callback = sess.callback;
  memset(&callback, 0, sizeof(callback));

  if (!is_last_stage) {
    callback.output_callback = stage_output_callback;
    callback.output_userdata = &sess.callback_ctx;
    callback.output_tensors = stage.output_tensors;
    callback.n_output_tensors = stage.n_output_tensors;
  }

  callback.tokenizer_callback = tokenizer_callback;
  callback.tokenizer_userdata = tokenizer;
  callback.embed_callback = embed_callback;
  callback.embed_userdata = &sess.embed_ctx;
  callback.result_callback = result_callback;
  callback.result_userdata = &conv.result;

  // 如果提供了 safetensors (rope caches)，则注册 input_callback 并设置 ext input indices
  if (input_cb_data && input_cb_data->rope_mmap_base) {
    callback.input_callback = input_callback;
    callback.input_userdata = input_cb_data;
    callback.input_tensors_index = stage.ext_input_indices;
    callback.n_input_tensors     = stage.n_ext_inputs;
    for (int i = 0; i < stage.n_ext_inputs; ++i) {
      VLOG("[%s] ext input tensor[%d] index=%d\n", stage.name.c_str(), i, stage.ext_input_indices[i]);
    }
  }

  int ret = rknn3_session_set_callback(sess.session, &sess.callback);
  sess.has_callback = (ret == RKNN3_SUCCESS);
  if (ret != RKNN3_SUCCESS) {
    printf("[%s] rknn3_session_set_callback failed, ret=%d\n", stage.name.c_str(), ret);
    return false;
  }

  printf("[%s] session init done (stage_idx=%zu)\n", stage.name.c_str(), stage_idx);
  return true;
}

static bool wait_stage_batch(StageSlot& slot, StageBatch* batch)
{
  std::unique_lock<std::mutex> lock(slot.mutex);
  slot.cv.wait(lock, [&slot]() {
    return !slot.batches.empty() || slot.producer_done || slot.failed;
  });

  if (slot.failed || slot.batches.empty()) {
    return false;
  }
  *batch = std::move(slot.batches.front());
  slot.batches.pop_front();
  return true;
}

static void run_stage_worker(size_t stage_idx, std::vector<StageContext>& stages,
                             Conversation& conv, const rknn3_llm_infer_param& infer_param,
                             InferencePhase phase)
{
  StageContext& stage = stages[stage_idx];
  PipelineState& pipeline = conv.pipeline;
  rknn3_session* session = conv.stages[stage_idx].session;
  StageSlot& input_slot = *pipeline.slots[stage_idx - 1];
  StageSlot* output_slot = stage_idx + 1 < stages.size() ? pipeline.slots[stage_idx].get() : nullptr;
  bool is_last_stage = (stage_idx == stages.size() - 1);
  uint64_t consumed_tokens = 0;

  while (true) {
    StageBatch batch;
    if (!wait_stage_batch(input_slot, &batch)) {
      std::lock_guard<std::mutex> lock(input_slot.mutex);
      if (!input_slot.failed && input_slot.producer_done && input_slot.batches.empty()) {
        break;
      }
      if (output_slot) {
        close_stage_slot(*output_slot);
      }
      return;
    }

    const TensorBlob* embed = pick_embed_tensor(batch.tensors);
    if (!embed || batch.n_tokens == 0 || stage.embedding_dim <= 0) {
      printf("[stage%zu] invalid pipeline batch\n", stage_idx);
      fail_pipeline(pipeline);
      return;
    }

    size_t embed_bytes = batch.n_tokens * (uint64_t)stage.embedding_dim * sizeof(float16);
    if (embed_bytes > embed->data.size()) {
      printf("[stage%zu] embed buffer too small: need=%zu, got=%zu\n",
             stage_idx, embed_bytes, embed->data.size());
      fail_pipeline(pipeline);
      return;
    }

    if (output_slot) {
      std::lock_guard<std::mutex> lock(output_slot->mutex);
      output_slot->expected_tokens += batch.n_tokens;
      output_slot->active_input_tokens = batch.n_tokens;
    }

    rknn3_llm_input embed_input;
    memset(&embed_input, 0, sizeof(embed_input));
    embed_input.input_type = RKNN3_LLM_INPUT_EMBED;
    embed_input.llm_input.embed = (float16*)embed->data.data();
    embed_input.llm_input.n_tokens = batch.n_tokens;
    rknn3_llm_infer_param local_param = infer_param;
    if (is_last_stage) {
      // 最后一段：根据 embed_callback 中统计的总 token 数判断是否最后一桶
      uint64_t total_tokens;
      {
        std::lock_guard<std::mutex> lock(pipeline.slots[0]->mutex);
        total_tokens = pipeline.slots[0]->expected_tokens;
      }
      local_param.disable_sampling = (consumed_tokens + batch.n_tokens < total_tokens);
    }
    consumed_tokens += batch.n_tokens;
    VLOG("[stage%zu] consume batch: n_tokens=%llu, bytes=%zu, disable_sampling=%d\n",
         stage_idx, (unsigned long long)batch.n_tokens, embed_bytes,
         (int)local_param.disable_sampling);
    timeval run_start;
    timeval run_end;
    int ret = RKNN3_SUCCESS;
    {
      // 卡级串行：持锁覆盖整个 session_run，回调里读 output_tensors 也在锁内。
      // 计时放在锁内，统计到的是纯 NPU 时间，等锁的时间只体现在并发模式的墙钟里。
      std::lock_guard<std::mutex> card_lock(stage.run_mutex);
      const uint64_t busy_t0 = stat_now_us();
      gettimeofday(&run_start, NULL);
      ret = rknn3_session_run(session, &embed_input, 1, &local_param);
      gettimeofday(&run_end, NULL);
      const uint64_t busy_t1 = stat_now_us();
      const double run_us = elapsed_us(run_start, run_end);
      record_stage_performance(stage, phase, batch.n_tokens, run_us / 1e3);
      // 资源观测。区间与 record_stage_performance 一模一样，都在锁内。
      //
      // ⚠️ 忙时用的是**单调钟**、不是上面那个 gettimeofday 算出来的 run_us，尽管两者
      // 量的是同一个区间：忙时最后要跟 STAT 的 `now_us`（也是单调钟）相除算利用率，
      // 中间被 NTP 校一次表的话，分子是 epoch 口径、分母是单调口径，比值就是荒谬的
      // （可能算出负数或几百个百分点）。gettimeofday 那一对留着不动，是因为
      // record_stage_performance 的历史口径就是它，改了历史数字就不可比了。
      record_stage_run_stat(stage, busy_t1 - busy_t0);
    }

    if (output_slot) {
      std::lock_guard<std::mutex> lock(output_slot->mutex);
      output_slot->active_input_tokens = 0;
    }
    if (ret != RKNN3_SUCCESS) {
      printf("[stage%zu] run failed ret=%d\n", stage_idx, ret);
      fail_pipeline(pipeline);
      return;
    }
  }

  if (output_slot) {
    close_stage_slot(*output_slot);
  }
}

static bool run_pipeline_once(std::vector<StageContext>& stages, Conversation& conv,
                              const char* prompt, const std::vector<int32_t>* input_tokens,
                              InferencePhase phase, uint64_t* stage0_input_tokens)
{
  if (stages.empty()) {
    return false;
  }
  PipelineState& pipeline = conv.pipeline;

  rknn3_llm_infer_param infer_param;
  memset(&infer_param, 0, sizeof(infer_param));
  infer_param.keep_history = 1;
  infer_param.max_new_tokens = 1;
  // 使用 prefill_only 模式进行单步推理
  infer_param.prefill_only = true;
  // 只有最后一段才进行采样，且prefill时只有最后一桶结束才采样
  infer_param.disable_sampling = true;
  if (stages.size() == 1) {
    // 单卡时 stage0 同时是最后一段，没有 worker 负责打开采样。
    infer_param.disable_sampling = false;
  }

  reset_pipeline(pipeline);
  reset_last_stage_result(conv);

  rknn3_llm_input first_input;
  memset(&first_input, 0, sizeof(first_input));
  if (prompt) {
    first_input.input_type = RKNN3_LLM_INPUT_PROMPT;
    first_input.llm_input.prompt = prompt;
  } else {
    if (!input_tokens || input_tokens->empty()) {
      return false;
    }
    first_input.input_type = RKNN3_LLM_INPUT_TOKEN;
    first_input.llm_input.tokens = const_cast<int32_t*>(input_tokens->data());
    first_input.llm_input.n_tokens = input_tokens->size();
  }

  std::vector<std::thread> workers;
  workers.reserve(stages.size() - 1);
  for (size_t i = 1; i < stages.size(); ++i) {
    workers.emplace_back(run_stage_worker, i, std::ref(stages), std::ref(conv),
                         std::cref(infer_param), phase);
  }

  timeval stage0_start;
  timeval stage0_end;
  int ret = RKNN3_SUCCESS;
  {
    // 与 worker 同源：stage0 也要拿本卡的串行锁（多会话时别的会话也在抢这张卡）。
    std::lock_guard<std::mutex> card_lock(stages[0].run_mutex);
    const uint64_t busy_t0 = stat_now_us();
    // 在锁内读本次 run 的忙时基线，并打开实时窗口：embed_callback 会在 run 进行中
    // 用 `base + (now - busy_t0)` 增量刷新 stat_busy_us（见其注释）。
    const uint64_t stage0_busy_base = stages[0].stat_busy_us.load(std::memory_order_relaxed);
    stages[0].stat_busy_base_us.store(stage0_busy_base, std::memory_order_relaxed);
    stages[0].stat_live_start_us.store(busy_t0, std::memory_order_relaxed);
    gettimeofday(&stage0_start, NULL);
    ret = rknn3_session_run(conv.stages[0].session, &first_input, 1, &infer_param);
    gettimeofday(&stage0_end, NULL);
    const uint64_t busy_t1 = stat_now_us();
    // 这里是 session_run 的**第二个**调用点：stage0 不走 run_stage_worker，是就地跑在
    // 本函数里的（其余各段才由 worker 跑）。两个调用点都要记观测——漏掉这个的后果
    // 就是"第 0 张卡永远是一张空卡"（见 record_stage_run_stat 的注释）。
    // 用 absolute 版本：STAT 合成时会用 base + (now - live_start)，这里把结算值定死成
    // 同一段时长的绝对累计（而不是再 fetch_add，免得重复计）。
    // **先结算、后关窗口**：反过来的话，关闭与结算之间的一次 STAT 会读到"窗口已关、
    // 忙时还是旧值"，滑窗一减就是负的。先结算则两种读到哪个都不小于上一次。
    record_stage_run_stat_absolute(stages[0], stage0_busy_base + (busy_t1 - busy_t0));
    stages[0].stat_live_start_us.store(0, std::memory_order_relaxed);
  }
  if (ret != RKNN3_SUCCESS) {
    printf("[stage0] run failed ret=%d\n", ret);
    fail_pipeline(pipeline);
  }
  close_stage_slot(*pipeline.slots[0]);

  for (auto& worker : workers) {
    worker.join();
  }

  uint64_t current_stage0_input_tokens = 0;
  {
    std::lock_guard<std::mutex> lock(pipeline.slots[0]->mutex);
    current_stage0_input_tokens = pipeline.slots[0]->expected_tokens;
  }
  {
    // 统计累加也必须串行，否则多个会话线程同时 += 会丢更新。
    std::lock_guard<std::mutex> card_lock(stages[0].run_mutex);
    record_stage_performance(stages[0], phase, current_stage0_input_tokens,
                             elapsed_us(stage0_start, stage0_end) / 1e3);
  }

  if (stage0_input_tokens) {
    *stage0_input_tokens = current_stage0_input_tokens;
  }

  return ret == RKNN3_SUCCESS && !pipeline_failed(pipeline);
}

struct ChatTurnResult
{
  uint64_t prefill_tokens = 0;
  uint64_t decode_tokens = 0;
  double   prefill_ms = 0.0;
  double   decode_ms = 0.0;
  // 这一轮被网关切断了（`STOP <rid>` 那一帧）。**必须单独带出来**：服务模式下
  // finish_reason 是拿 decode_tokens 与上限反推的，而一轮被停掉时它一定小于上限，
  // 反推出来就是 stop——页面上从此分不出「模型自己说完了」和「用户按了暂停」，
  // 而那正是用户接下来要决定「继续」还是「问别的」的依据。
  bool     aborted = false;
};

// 运行一轮完整的对话：以 prompt 做 prefill，再逐 token decode 生成本轮回复。
// 历史通过 run_pipeline_once 内部的 keep_history=1 自动累积，本函数不清理 KV cache。
// `cancel` 是本轮的**中止标志**（网关发一帧 `STOP <rid>`，输入线程把它置 true）。
// 传指针而不是传值：置位的是另一个线程，只有在下一个 token 边界上重新读才看得见。
// 交互模式与本地路径不传（默认 nullptr）——那条路上没有人会发 STOP。
static bool run_chat_turn(std::vector<StageContext>& stages, Conversation& conv,
                          const VocabInfo& vocab_info, const char* prompt,
                          int max_new_tokens, ChatTurnResult* result,
                          const std::atomic<bool>* cancel = nullptr,
                          bool resume = false)
{
  // 本轮还没开工就被叫停（STOP 赶在这一轮 prefill 之前到了）：整轮不发 token、
  // **一个字节都不碰设备**。KV 没动过，所以下面 DONE 报 0/0 与 KV 的真实状态一致。
  // 不做这个检查的话，这一轮会照样把整段 prompt prefill 进去（长 prompt 要几秒），
  // 而暂停在最需要它生效的时刻——等了几秒还没出字——恰恰不生效。
  if (cancel != nullptr && cancel->load()) {
    if (result) {
      result->aborted = true;
    }
    return true;
  }

  timeval prefill_start;
  timeval prefill_end;
  gettimeofday(&prefill_start, NULL);
  uint64_t prefill_tokens = 0;
  int       next_token = -1;
  if (resume) {
    // 继续上一轮被打断的 decode：**不 prefill**。KV 已经装着「上一轮的 prompt + 已生成
    // 的 token」，再 prefill 一遍（keep_history 是追加）等于把历史双份塞进 KV。直接读
    // 上一轮 STOP 留下的那个 token（已生成、已上报、还没喂回 KV），从它开始接着 decode。
    //
    // 为什么这个 token 一定还在：STOP 停在 decode 循环顶上，run_chat_turn 返回时没有
    // 再碰过 conv.result（reset_last_stage_result 只在 run_pipeline_once 里被调），而
    // RESUME 又是这个会话的**下一条**活——中间没有别的 prefill 把它清掉。
    get_last_stage_token(conv, &next_token);
    if (next_token < 0) {
      printf("resume did not find a token to continue from\n");
      return false;
    }
    gettimeofday(&prefill_end, NULL);
  } else {
    if (!run_pipeline_once(stages, conv, prompt, nullptr,
                           InferencePhase::PREFILL, &prefill_tokens)) {
      printf("prefill failed\n");
      return false;
    }
    gettimeofday(&prefill_end, NULL);
    get_last_stage_token(conv, &next_token);
    if (next_token < 0) {
      printf("prefill did not return token from result_callback\n");
      return false;
    }
  }

  timeval decode_start;
  timeval decode_end;
  gettimeofday(&decode_start, NULL);
  uint64_t decode_tokens = 0;
  uint64_t decode_steps = max_new_tokens > 0 ? (uint64_t)max_new_tokens : 0;
  for (uint64_t step = 0; step < decode_steps && next_token >= 0; ++step) {
    // 网关要求停这一轮（网页上的「暂停回答」）。**检查点必须在这个位置**：
    // 上一个 token 已经交给回调、也已经计入 decode_tokens，从这里 break 出去，
    // KV 里装着什么与 我们报了多少 是同一个数（板上的 finish_reason=abort
    // 也是靠这个数才说得通）。放到 run_pipeline_once **后面**就晚了：那一次
    // forward 已经跑完，会多报一个 token 出去，而那个 token 客户端也收得到。
    if (cancel != nullptr && cancel->load()) {
      if (result) {
        result->aborted = true;
      }
      break;
    }
    std::vector<int32_t> token_vec(1, next_token);
    VLOG("[Decode %llu] token=%d\n", (unsigned long long)(step + 1), next_token);

    if (!run_pipeline_once(stages, conv, nullptr, &token_vec,
                           InferencePhase::DECODE, nullptr)) {
      printf("decode step %llu failed\n", (unsigned long long)(step + 1));
      break;
    }

    if (!get_last_stage_token(conv, &next_token)) {
      printf("decode step %llu did not return token from result_callback\n",
             (unsigned long long)(step + 1));
      break;
    }
    decode_tokens += 1;
    if (!g_ignore_eos && next_token == vocab_info.special_eos_id[0]) {
      VLOG("\ndecode step %llu reached EOS token\n", (unsigned long long)(step + 1));
      break;
    }
  }
  gettimeofday(&decode_end, NULL);

  if (result) {
    result->prefill_tokens = prefill_tokens;
    result->decode_tokens = decode_tokens;
    result->prefill_ms = elapsed_us(prefill_start, prefill_end) / 1e3f;
    result->decode_ms = elapsed_us(decode_start, decode_end) / 1e3f;
  }
  return true;
}

// ============================================================================
// 多会话并发执行器（--sessions N）
//
// 每个会话一个驱动线程，各自跑 M 轮 prefill+decode。会话之间**唯一的**共享资源是
// 每张卡的那一份 context + output_tensors，由 StageContext::run_mutex 串行化；
// 会话自己的 KV / 流水线队列 / 统计都是独立的。
//
// 为什么并发是安全的、以及为什么串行化每张卡仍然有收益，见 StageContext::run_mutex
// 上的注释。
//
// 正确性判据（不是「跑起来了」就算数）：
//   同一 prompt、同一 -n、--ignore-eos，N=2 的两个会话各自 dump 出来的 token id
//   必须与单会话路径逐 token 完全一致。任何跨会话串扰（output_tensors 被覆盖、
//   KV 混用、队列串台）都会让 token 变掉。
// ============================================================================

// 让 N 个驱动线程尽量同时起跑。不设这个闸门的话，先起的会话会白跑一段，
// 后起的会话还没开始，量出来的吞吐会虚高。
struct StartGate
{
  std::mutex              mutex;
  std::condition_variable cv;
  int                     arrived = 0;
  int                     total = 0;
  bool                    open = false;

  explicit StartGate(int n) : total(n) {}

  void arrive_and_wait()
  {
    std::unique_lock<std::mutex> lock(mutex);
    if (++arrived >= total) {
      open = true;
      cv.notify_all();
      return;
    }
    cv.wait(lock, [this]() { return open; });
  }
};

struct SessionTiming
{
  timeval start{};
  timeval end{};
  bool    valid = false;
};

// 清掉一个会话在所有卡上的 KV，并把它的对话状态复位（下次 prefill 会重新带 system 轮）。
//
// 逐卡取锁、取一张放一张（§3.4 锁序不变量）：SDK 要求「同一个 rknn3_context 的并发
// 使用由调用方保证线程安全」，而本会话的 session 和别的会话的 session 同处一张卡的
// **同一个 context** 上——清 KV 虽然只动本会话自己的 KV 缓冲（P0 探针已证 KV 隔离），
// 但它仍是对该 context 的一次 SDK 调用，可能与另一路正在跑的 session_run 并发。
//
// **这是全文件唯一的清 KV 实现，五处调用点都走它**（上下文将满时自动清 ×2、
// 网关请求的 RESET、两处收尾清理）。放在这里而不是放在第一个调用点旁边，是因为
// `Conversation` 得先定义完——helper 抽出来了却只有两处用它、另外三处还各抄一份持锁
// 循环，等于把"漏掉某一边的锁"的风险原样留着（2026-09-16 改）。
//
// 收尾那两处（所有驱动线程 join 之后）其实没有并发、不需要锁；照样走这里是为了
// **不留第二份实现**——多一份就多一处将来只改一半的地方。它们顺带把会话状态复位，
// 紧接着会话就被销毁，无影响。
static void clear_conversation_kv(std::vector<StageContext>& stages, Conversation& conv)
{
  for (size_t i = 0; i < stages.size(); ++i) {
    std::lock_guard<std::mutex> card_lock(stages[i].run_mutex);
    rknn3_session_clear_kvcache(conv.stages[i].session, RKNN3_KVCACHE_CLEAR_ALL);
  }
  conv.context_tokens = 0;
  conv.first_turn = true;
}

// 改动 C：多卡下每段 KV 各存一个文件，命名与官方 server 的约定一致——
// `task.kv` -> `task_seg0.kv` / `task_seg1.kv` / ...（在扩展名前插入 _segN）。
static std::string stage_kvcache_path(const std::string& base, size_t stage)
{
  const std::string suffix = "_seg" + std::to_string(stage);
  const size_t dot = base.find_last_of('.');
  const size_t slash = base.find_last_of("/\\");
  if (dot != std::string::npos && (slash == std::string::npos || dot > slash)) {
    return base.substr(0, dot) + suffix + base.substr(dot);
  }
  return base + suffix + ".kv";
}

// 改动 C：把本会话在每张卡上的 KV 各存一份（换了页的空闲会话可以被收回内存，需要时
// 再 load 回来，免去重新 prefill）。**只由会话自己的驱动线程调用**：它逐卡取 run_mutex，
// 与同卡上的推理串行；输入线程直接下手会和别的 stage 交错，存出半个 token 的 KV。
static bool save_conversation_kvcache(std::vector<StageContext>& stages, Conversation& conv,
                                      const std::string& path)
{
  for (size_t i = 0; i < stages.size(); ++i) {
    std::lock_guard<std::mutex> card_lock(stages[i].run_mutex);
    if (!conv.stages[i].session) {
      return false;
    }
    const std::string p = stage_kvcache_path(path, i);
    if (rknn3_session_save_kvcache(conv.stages[i].session, p.c_str()) != 0) {
      return false;
    }
  }
  return true;
}

static bool load_conversation_kvcache(std::vector<StageContext>& stages, Conversation& conv,
                                      const std::string& path)
{
  for (size_t i = 0; i < stages.size(); ++i) {
    std::lock_guard<std::mutex> card_lock(stages[i].run_mutex);
    if (!conv.stages[i].session) {
      return false;
    }
    const std::string p = stage_kvcache_path(path, i);
    if (rknn3_session_load_kvcache_from_path(conv.stages[i].session, p.c_str()) != 0) {
      return false;
    }
  }
  return true;
}

// 一个会话的驱动循环。首轮用 first_prompt（与单会话路径同源，便于逐 token 对比），
// 后续轮用模板拼出的续写 prompt。
static void run_conversation_worker(Conversation& conv, std::vector<StageContext>& stages,
                                    const VocabInfo& vocab_info, const ChatTemplateSpec* tpl,
                                    const char* first_prompt, int max_new_tokens, int rounds,
                                    uint64_t context_limit, StartGate* gate,
                                    SessionTiming* timing)
{
  gate->arrive_and_wait();
  gettimeofday(&timing->start, NULL);

  const uint64_t prefill_reserve = 512;
  std::string    round_prompt;

  for (int round = 0; round < rounds; ++round) {
    if (conv.context_tokens > 0 && context_limit > 0 &&
        conv.context_tokens + prefill_reserve >= context_limit) {
      // 与交互模式同一条策略：快满了就清 KV 重开一段对话（只影响本会话）。
      // 清 KV 的取锁规矩在 clear_conversation_kv 里（逐卡取、取一张放一张）。
      //
      // 用 VLOG 而不是 printf：--rounds 是已验收路径，默认输出必须逐字节不变。
      // 但这条清 KV 分支原本**没有任何日志**，测试时无法证明它真被走到了（这正是
      // 之前 30 分钟稳定性测试的盲区：那轮 workload 根本没到清 KV 的阈值）。
      // --verbose 打开即可计数。
      VLOG("[conv] context %llu/%llu nearly full, clearing KV cache\n",
           (unsigned long long)conv.context_tokens, (unsigned long long)context_limit);
      clear_conversation_kv(stages, conv);
    }

    // 首轮直接用 --prompt 原文（与单会话路径同源，token id 才具备可比性）；
    // 后续轮用 chat 模板拼一个续写 prompt。刚清过 KV 时按新对话的开头处理，
    // 把 system prompt 补回来。
    if (round == 0) {
      round_prompt = first_prompt;
    } else {
      if (conv.first_turn) {
        round_prompt = tpl->system_prompt;
      }
      round_prompt += tpl->user_prefix;
      round_prompt += "continue";
      round_prompt += tpl->user_postfix;
    }
    conv.first_turn = false;

    const char* prompt_for_round = round_prompt.c_str();

    ChatTurnResult turn;
    if (!run_chat_turn(stages, conv, vocab_info, prompt_for_round, max_new_tokens, &turn)) {
      conv.failed = true;
      break;
    }
    conv.total_prefill_tokens += turn.prefill_tokens;
    conv.total_decode_tokens += turn.decode_tokens;
    conv.total_prefill_ms += turn.prefill_ms;
    conv.total_decode_ms += turn.decode_ms;
    conv.context_tokens += turn.prefill_tokens + turn.decode_tokens;
  }

  gettimeofday(&timing->end, NULL);
  timing->valid = true;
  conv.wall_ms = elapsed_us(timing->start, timing->end) / 1e3;
}

static uint64_t timeval_to_us(const timeval& tv)
{
  return (uint64_t)tv.tv_sec * 1000000ULL + (uint64_t)tv.tv_usec;
}

// ============================================================================
// 交互式多会话（--sessions N --interactive）
//
// 与 --rounds 路径的区别只有一处：输入来自 stdin，而且是**不定时**来的。
//
// 分发策略「谁空闲给谁」：N 个会话驱动线程共用一条待办队列，谁先从队列里取到输入，
// 谁就处理这一轮——"当时空闲"由「能从队列里取到活」直接定义，不需要另外维护一张
// 忙/闲状态表。额外的状态表反而有窗口：某个会话刚跑完、还没来得及把自己标回空闲时，
// 输入会被误分给一个其实不空闲的会话。
//
// 输出策略「整段成块 + 前缀」：一轮回复的全部文本先攒在会话私有的 buffer 里
// （LastStageResultState::out 指向的 TokenSink），跑完再整体加 `[s<i>]` 前缀一次性打出。
// 逐 token 直接打 stdout 在 N 路并发下必然互相穿插，读不出哪句属于哪个会话。
// ============================================================================

// 待办输入的容量上限。不设上限的话，往 stdin 里灌一大段文本会让待办无限膨胀，
// 而每个会话的 KV 只装得下有限轮上下文。满了就让输入线程等一会（反压）。
static const size_t kInteractivePendingCap = 32;

// 待办队列里的一条活。交互模式只用 text；服务模式四个字段都用。
struct PendingInput
{
  std::string text;
  // 以下三个字段只有服务模式会写，交互模式保持默认。
  //
  // target_session 是服务模式与交互模式唯一的语义差别：交互模式一律 -1（谁空闲给谁），
  // 服务模式会指定会话。为什么非要能指定——网关要在会话上粘住一段对话的 KV，
  // 只有把「同一段对话」钉死在同一个会话上，多轮之间才能只补差异部分做 prefill；
  // 否则每轮都得重发全量 prompt 再清 KV 重算，长上下文下每轮多花上百秒。
  int         target_session = -1;
  int         max_new_tokens = 0;   // 0 = 用进程默认值（--n-predict）
  uint64_t    rid = 0;              // 请求 id，原样回显在回复的每一帧上
  // 本轮开始前先把本会话的 KV 清空。网关把新对话重新分到一个还留着上一段对话 KV 的
  // 会话上时用它——不清就会把上一段对话的上下文接在后面，答出来的东西是串味的。
  // 清空动作必须由会话自己的驱动线程做（它按卡取锁，见 clear_conversation_kv），
  // 所以它是请求上的一个标志位，而不是输入线程可以直接执行的独立命令。
  bool        reset = false;
  // 改动 C（会话换页）：这条待办是"跑一轮"，还是"把本会话 KV 存到文件 / 从文件恢复"。
  // 存/取同样**必须由会话自己的驱动线程执行**——它按卡取锁，与正在跑的推理串行；
  // 输入线程直接下手会和别的 stage 交错，存出一份半个 token 的 KV。所以也是一种 kind。
  enum class Kind
  {
    kRun,     // 普通推理（默认）
    kSave,    // SAVE：把本会话 KV 存到 kvcache_path
    kLoad,    // LOAD：从 kvcache_path 恢复到本会话
  };
  Kind        kind = Kind::kRun;
  std::string kvcache_path;
  // LOAD 时携带：这份 KV 对应多少 token（网关记账值）。恢复后写回 conv.context_tokens，
  // 否则后端的内存闸会以为 KV 是空的，下一轮的"将满"判断就形同虚设。
  uint64_t    context_tokens_hint = 0;
  // 继续上一轮被打断的 decode（`RESUME <rid> ...`）。与 REQ 的差别只有这一个标志位：
  // 入队、登记、派发、注销走的是同一条路，只是 run_chat_turn 拿到它之后跳过 prefill。
  bool        resume = false;
  // 这一轮的中止标志（`STOP <rid>`）。用 shared_ptr 而**不是**内嵌一个 atomic：
  // 输入线程要在这一条已经离开队列、正被会话线程跑着的时候找到同一个对象，
  // dispatcher->inflight 里按 rid 存的就是这个 shared_ptr；job 在队列与孤儿清扫
  // 之间被拷进拷出时，标志位始终是同一个。
  std::shared_ptr<std::atomic<bool>> cancel;
};

struct InteractiveDispatcher
{
  std::mutex              mutex;
  std::condition_variable cv_work;   // 会话线程等：有自己能接的活，或输入已结束
  std::condition_variable cv_space;  // 输入线程等：待办队列没满
  std::deque<PendingInput> pending;
  bool                    input_eof = false;
  // 输入线程自己读到 EOF 而退出。与 input_eof 分开：input_eof 也会被「会话全部
  // 退出」这种情况置位，两者的语义不同，混用会误判输入线程是否还卡在 getline 上。
  bool                    input_finished = false;
  // 还活着的会话线程数。用于兜住一种会挂死的收尾情况：所有会话都因推理失败
  // 提前退出，而输入线程正卡在「待办队列满了」的反压等待上——没人会再来消费
  // 队列，它就永远等下去。归零时把输入线程一并放行。
  int                     active_workers = 0;
  // 每个下标上的会话线程是否还活着（下标 = 会话号，启动时全 true）。
  //
  // 为什么非要有这一张表：服务模式下「钉给会话 i」的活**只有 i 能接**（见
  // pending_takeable），所以 i 的线程一退出，钉给它的活就再没有任何人能取——
  // 它们既不会回 DONE 也不会回 ERR，而是永远躺在队列里占额度；攒满
  // kInteractivePendingCap 之后输入线程卡死在 cv_space 上（别的会话还活着，
  // active_workers != 0，那个放行条件永远不成立）→ 网关侧管道写满 → 整条链停摆，
  // 连进程都退不出来（input_thread.join() 等不到）。
  //
  // 注意是"部分失败才卡"：全部会话都退出时 active_workers 归零会把输入线程放行，
  // 所以只跑成功路径的演示永远测不出这条。
  std::vector<bool> worker_alive;
  // rid → 这一轮的中止标志（`STOP <rid>`，见 run_chat_turn 的 cancel 参数）。
  // 输入线程在入队前登记、收到 STOP 时置位、会话线程跑完之后注销，**三处都在
  // 上面这把 mutex 下**——所以「会话线程正在跑」与「输入线程把它停掉」这两件事
  // 之间没有窗口。
  //
  // 光有 PendingInput::cancel 还不够：入了队之后 job 就被会话线程取走了，输入线程
  // 手里不再有它——而 STOP 只可能在 REQ 之后到（两者走同一条有序的 stdin）。
  std::map<uint64_t, std::shared_ptr<std::atomic<bool>>> inflight;
};

// 交一条待办给会话线程的结果。**三个状态而不是一个 bool**：服务模式下「这条活的目标
// 会话已经死了」和「所有会话都没了」后果完全不同——前者只该给这一条请求回 ERR，
// 然后继续收后面的活；后者说明这个进程已经没有消费方，输入线程该收工了。合成一个
// false 会让一次坏会话把整个服务带走。
enum class PushResult
{
  kOk,            // 收下了
  kDeadTarget,    // 目标会话已死：这条活没人会跑，请求方在等一个永远不来的答案
  kWorkersGone,   // 所有会话都退了：输入线程收工
};

// 某个会话线程能不能接这一条待办。交互模式下所有待办的 target_session 都是 -1，
// 对谁都可接，于是「取队首」——与改动前行为完全一致；服务模式下被钉住的活只有
// 目标会话能接，别的会话必须绕过去接着等（不是丢给别人跑）。
static bool pending_takeable(const PendingInput& item, int session_index)
{
  return item.target_session < 0 || item.target_session == session_index;
}

// 整段输出（回复块、或本会话的系统提示）都走这里，保证不会两段撞在一行中间。
static void print_session_block(int index, const std::string& text)
{
  std::lock_guard<std::mutex> lock(g_output_mutex);
  printf("[s%d] %s\n", index, text.c_str());
  fflush(stdout);
}

// 一个会话的交互/服务驱动循环：取到一条活 → 跑完整一轮 → 交回落点 → 回去接着等。
// 两个前端共用本函数：--interactive 把整段回复打到 stdout，--serve 发成 DELTA/DONE 帧。
static void run_interactive_session_worker(int index, Conversation& conv,
                                           std::vector<StageContext>& stages,
                                           const VocabInfo& vocab_info,
                                           const ChatTemplateSpec* tpl,
                                           int max_new_tokens, uint64_t context_limit,
                                           InteractiveDispatcher* dispatcher,
                                           SessionTiming* timing)
{
  // 计时从**第一轮真正开工**起算，而不是本线程启动的那一刻。交互模式下输入是不
  // 定时来的，从线程启动起算会把用户思考/打字的时间算进并发窗口，聚合出来的
  // tok/s 就会随人多想几秒而变——那个数就不是吞吐了。
  bool           timing_started = false;
  const uint64_t prefill_reserve = 512;
  PendingInput   job;

  for (;;) {
    {
      std::unique_lock<std::mutex> lock(dispatcher->mutex);
      // 等待条件必须带上「有我能接的活」：服务模式下队列里会有钉给别的会话的活，
      // 那种活在本线程眼里等于不存在，不能因为「队列非空」就醒过来空转一场。
      dispatcher->cv_work.wait(lock, [dispatcher, index]() {
        if (dispatcher->input_eof && dispatcher->pending.empty()) {
          return true;
        }
        for (const auto& item : dispatcher->pending) {
          if (pending_takeable(item, index)) {
            return true;
          }
        }
        return dispatcher->input_eof;
      });
      bool taken = false;
      for (auto it = dispatcher->pending.begin(); it != dispatcher->pending.end(); ++it) {
        if (pending_takeable(*it, index)) {
          job = *it;
          dispatcher->pending.erase(it);
          taken = true;
          break;
        }
      }
      if (!taken) {
        break;  // 输入已结束，且队列里没有钉给本会话的活：收工
      }
      // 唤醒**所有**等待者而不是一个：被唤醒的那一个不一定接得住队首
      // （队首可能正钉给别人的会话），notify_one 会把这一条活晾在那里没人取。
      dispatcher->cv_space.notify_all();  // 队列腾出位置，放行输入线程
      dispatcher->cv_work.notify_all();
    }

    // 改动 C：KV 存/取作业（会话换页）。就地做完回一帧，**不计入推理计时**——它不是
    // 一轮对话。save/load 由本线程执行，天然与推理串行，也不会和别的 stage 交错。
    if (job.kind != PendingInput::Kind::kRun) {
      const bool is_save = (job.kind == PendingInput::Kind::kSave);
      const bool ok = is_save ? save_conversation_kvcache(stages, conv, job.kvcache_path)
                              : load_conversation_kvcache(stages, conv, job.kvcache_path);
      if (ok && !is_save) {
        // load 后 KV 里装着网关记账的那段对话：写回 token 数，内存闸才准。
        conv.context_tokens = job.context_tokens_hint;
        conv.first_turn = (job.context_tokens_hint == 0);
      }
      serve_op_result(is_save ? "SAVED " : "LOADED ", job.rid, ok ? 1 : 0);
      continue;
    }

    if (!timing_started) {
      gettimeofday(&timing->start, NULL);
      timing_started = true;
    }

    // 两种要清 KV 的情形，共用 clear_conversation_kv（按卡取锁那段在这里面）：
    //   · 网关明确要求（RESET）：新对话落到了一个还留着上一段 KV 的会话上；
    //   · 上下文将满：与单会话交互模式同一条策略，只影响本会话。
    // 服务模式下清 KV 不是"提示"而是**语义事件**——本会话的 KV 一丢，粘在它上面的
    // 那段对话历史就没了。网关必须知道，否则它会以为上下文还在、下一轮只补差异部分，
    // 模型看到的是一段残缺的对话。所以这里发 CLEAR 帧，带上被丢弃的 token 数。
    if (g_serve_mode && job.reset && conv.context_tokens > 0) {
      serve_clear(job.rid, index, conv.context_tokens, context_limit);
      clear_conversation_kv(stages, conv);
    }

    // 本轮要往 KV 里放的 token 总量 = 已经在那儿的 + 这一段 prompt。
    // **服务模式下这一段必须自己数**：网关只发增量（`prompt[len(base):]`，板上实测
    // sent=84 / full=522），增量多长完全由客户端决定，跟 conv.context_tokens 无关。
    // 只盯累计值的话两类请求都漏——「新会话的第一条就超」（累计值还是 0）和「聊到
    // 一半突然贴一份长文档/diff」（累计值看着很小）。它们会一路走到 prefill，在 SDK
    // 里以 input_tokens > max_position_embeddings 失败，回来的是 ERR 而不是 REJECT，
    // 网关于是 mark_dead——正是下面那段注释要防的后果，2026-09-17 上板实测撞到。
    // 交互模式不数：那边 prompt 由模板拼、长短就是本次输入，而交互模式的策略本来
    // 就是"自动清 KV 接着跑"（见最后的 else 分支），没有会死的会话。
    uint64_t incoming_tokens = 0;
    if (g_serve_mode && !job.resume) {
      const int n = count_prompt_tokens(conv.result.tokenizer, job.text);
      if (n > 0) {
        incoming_tokens = (uint64_t)n;
      } else {
        // 数不出来**不能当成 0**：这条判据是"装不下"的唯一拦截点，静默失效比报错
        // 难查得多（表现是偶发 503 + 会话一个个变少）。留一条日志，至少可查。
        printf("[session %d] WARNING: cannot count the incoming prompt; "
               "the context-limit guard is degraded for this turn\n",
               index);
        fflush(stdout);
      }
    }
    const uint64_t projected_tokens = conv.context_tokens + incoming_tokens;
    const bool context_almost_full =
        projected_tokens > 0 && context_limit > 0 &&
        projected_tokens + prefill_reserve >= context_limit;
    if (context_almost_full && g_serve_mode && !job.resume) {
      // **服务模式下不能"自动清 KV 再把这一轮跑完"**。上下文将满时清 KV 在交互模式
      // 里是合理的（整段对话都在本进程里拼，清完重来一轮语义自洽），但服务模式下
      // 网关只发增量（`prompt[len(base):]`，板上实测 sent=84 / full=522）：KV 一清，
      // 模型拿到的就是一段**没有开头**的对话，然后以 finish_reason=stop 返回一个
      // 自信的错答案。CLEAR 帧救的是**下一轮**（网关据此把这段对话的粘性记录作废、
      // 下轮全量重发），**本轮已经错了，而且自愈之后不留痕迹**——不报错、不重试、
      // 只错一轮，是最难查的一类。
      //
      // 所以改成**拒掉这一轮**，把策略交回网关：
      //   · 先发 CLEAR：让网关作废粘性记录（known[session] = None），下一轮必定
      //     RESET 全量重发，不会再用增量拼一段残缺历史；
      //   · 再发 REJECT：告诉网关「这个会话还活着，只是这一轮没跑」。**不能发 ERR**：
      //     网关收到 ERR 会把会话标死（mark_dead），而上下文装不下不是会话的错——
      //     标死等于每撞一次超限就永久少一个会话，四次之后整个服务没有会话可用。
      // 拒的理由分两种，客户端该采取的动作也完全不同，所以消息要分开写：
      //   · 这一轮 prompt 自己就装不下 —— 清 KV 没用（KV 本来就是空的），只能改请求；
      //   · 历史攒太长 —— 清 KV + 开新对话能救，下一轮 RESET 全量重发会短一些。
      const bool this_turn_alone = incoming_tokens + prefill_reserve >= context_limit;
      const std::string reason =
          this_turn_alone
              ? "this single turn is too long: " + std::to_string(incoming_tokens) +
                    " tokens of prompt alone (limit " + std::to_string(context_limit) +
                    "); shorten this request"
              : "context limit reached: " + std::to_string(conv.context_tokens) +
                    " tokens of history + " + std::to_string(incoming_tokens) +
                    " of this turn (limit " + std::to_string(context_limit) +
                    "); start a new conversation or trim the history";
      // CLEAR 报的必须是**真被丢掉的那些** token，也就是 conv.context_tokens——
      // 不能报 projected_tokens，本轮 prompt 一个 token 都还没进 KV（报了的话网关
      // 的 known[] 会比实际 KV 长，前缀判据从此对不上，缓存静默全废）。
      // 上面的 RESET 分支可能已经把 KV 清空了，那就没什么可报、也不用再清一遍
      // （清两遍不是错，只是白干一场）。
      if (conv.context_tokens > 0) {
        serve_clear(job.rid, index, conv.context_tokens, context_limit);
        // 顺手把 KV 清掉：这段对话本身就装不下，留着满 KV 只会让下一轮同样顶穿。
        // 清完是"确定为空"的状态，网关已知情（上面那帧 CLEAR），下一轮会 RESET 重发。
        clear_conversation_kv(stages, conv);
      }
      serve_reject(job.rid, index, reason);
      // continue 而不是 break：会话线程是好的，只是这一轮不跑。break 出去等于把一个
      // 健康的会话永久摘掉（服务容量少一份），钉给它的活从此没人接（见 worker_alive）。
      continue;
    } else if (context_almost_full) {
      print_session_block(index,
          "(context " + std::to_string(conv.context_tokens) + "/" +
          std::to_string(context_limit) +
          " tokens nearly full, clearing KV cache to start a fresh conversation)");
      clear_conversation_kv(stages, conv);
    }

    std::string chat_prompt;
    if (g_serve_mode) {
      // prompt 由网关按 OpenAI 的 messages 拼好（含 system 轮与 assistant 历史），
      // 这里逐字节透传：进程内的模板只有「首轮 / 后续轮」两态，表达不了任意角色的历史。
      chat_prompt = job.text;
    } else if (conv.first_turn) {
      // Gemma-4 没有 system role，其 system_prompt 为空串，这里自然退化为不带 system 轮。
      chat_prompt = tpl->system_prompt;
      chat_prompt += tpl->user_prefix;
      chat_prompt += job.text;
      chat_prompt += tpl->user_postfix;
      conv.first_turn = false;
    } else {
      chat_prompt = tpl->user_prefix;
      chat_prompt += job.text;
      chat_prompt += tpl->user_postfix;
    }

    StringTokenSink block_sink;
    ServeTokenSink  serve_sink(job.rid);
    conv.result.out = g_serve_mode ? static_cast<TokenSink*>(&serve_sink)
                                   : static_cast<TokenSink*>(&block_sink);
    const int turn_max_new_tokens =
        (g_serve_mode && job.max_new_tokens > 0) ? job.max_new_tokens : max_new_tokens;
    ChatTurnResult turn;
    const bool ok = run_chat_turn(stages, conv, vocab_info, chat_prompt.c_str(),
                                 turn_max_new_tokens, &turn, job.cancel.get(),
                                 job.resume);
    conv.result.out = nullptr;
    // 这一轮结束了（成功、失败、被停都算），把那条登记摘掉。**失败路径也必须摘**：
    // 下面是 break 出循环，漏掉就是一条永久泄漏，而且一点症状都没有。
    {
      std::lock_guard<std::mutex> lock(dispatcher->mutex);
      dispatcher->inflight.erase(job.rid);
    }

    // 注意：run_chat_turn 内部的失败诊断（"prefill failed" 等）仍是裸 printf，
    // 会插在别人的回复块之间——那是终止性错误路径，不值得为它把共享函数改复杂。
    // 这里再补一条带前缀的、明确属于本会话的记录。（服务模式下这些裸 printf 只脏
    // stdout 日志、冲不到 fd 3 上的帧流——这正是帧不走 stdout 的原因。）
    if (!ok) {
      conv.failed = true;
      if (g_serve_mode) {
        serve_err(job.rid, index,
                  block_sink.text.empty() ? "turn failed" : block_sink.text);
      } else {
        print_session_block(index, block_sink.text.empty()
                                       ? "*** turn failed"
                                       : block_sink.text + "\n*** turn failed");
      }
      break;
    }

    conv.turns_done += 1;
    conv.total_prefill_tokens += turn.prefill_tokens;
    conv.total_decode_tokens += turn.decode_tokens;
    conv.total_prefill_ms += turn.prefill_ms;
    conv.total_decode_ms += turn.decode_ms;
    conv.context_tokens += turn.prefill_tokens + turn.decode_tokens;

    if (g_serve_mode) {
      // finish_reason 只能由「生成了几个 token」反推：run_chat_turn 在步数用尽和采样到
      // 结束符两种情况下是同一个 break，没把原因带出来。不改它的签名（那条路径被逐字节
      // 验收过），按 decode_tokens 是否触顶判断——对 OpenAI 客户端来说够用。
      // abort **排在最前**：被停掉的一轮 decode_tokens 一定小于上限，光按下面那条
      // 反推会报成 stop，页面上就分不出「模型自己说完了」和「用户按了暂停」。
      const char* finish_reason =
          turn.aborted
              ? "abort"
              : ((turn_max_new_tokens > 0 &&
                  turn.decode_tokens >= (uint64_t)turn_max_new_tokens)
                     ? "length"
                     : "stop");
      serve_done(job.rid, index, finish_reason, turn.prefill_tokens, turn.decode_tokens,
                 turn.prefill_ms, turn.decode_ms, conv.context_tokens);
    } else {
      print_session_block(index, block_sink.text);
    }
  }

  gettimeofday(&timing->end, NULL);
  // 只有**真接到过活**的会话才让这条 timing 进汇总。没接到活的是 start{0,0}（零初始化）
  // + end=现在，于是 report_interactive_sessions 里那个 !t.valid 的过滤形同虚设，
  // min(start) 被拉成 0（1970）→ 窗口变成 1.78e9 秒 → 打印 "0.00 tok/s"，而同一行的
  // 状态还写着 "ok"。触发一点都不奇怪：少于 N 个会话被用到就 QUIT（服务模式下人为
  // 控制并发数、或交互模式输入行数少于会话数）。一行之差，直接把聚合数字变成垃圾。
  timing->valid = timing_started;
  conv.wall_ms = elapsed_us(timing->start, timing->end) / 1e3;

  // 会话线程退出登记（正常收工与推理失败两条路径都会走到这里，因为两个出口都是
  // break 出循环）。最后一个退出的人负责把输入线程也放行，见 active_workers 注释。
  bool all_gone = false;
  bool input_still_reading = false;
  std::vector<PendingInput> orphaned;
  {
    std::lock_guard<std::mutex> lock(dispatcher->mutex);
    // 先记下"本会话已经死了"，再把钉给它的活取出来。两件事必须在**同一把锁**里做：
    // 中间放开的话，输入线程正好能挤进一条钉给本会话的活，而它检查 worker_alive 时
    // 看到的还是 true —— 这条活就又成了永远没人接的孤儿。
    if (index >= 0 && (size_t)index < dispatcher->worker_alive.size()) {
      dispatcher->worker_alive[index] = false;
    }
    // 只有本线程能接的活（target_session == index）现在没人接了，取出来回 ERR。
    // 留在队列里的后果见 worker_alive 的注释：占额度 → 攒满 → 输入线程卡死 →
    // 整条服务链停摆。交互模式下 target_session 恒为 -1，这里扫不到东西。
    for (auto it = dispatcher->pending.begin(); it != dispatcher->pending.end();) {
      if (it->target_session == index) {
        orphaned.push_back(*it);
        it = dispatcher->pending.erase(it);
      } else {
        ++it;
      }
    }
    if (--dispatcher->active_workers == 0) {
      dispatcher->input_eof = true;
      all_gone = true;
      // 输入线程若正阻塞在 getline 上（只有终端会这样），上面那个 notify 是叫不醒
      // 它的——stdin 上的阻塞读没法被条件变量打断。此时进程会在 main 的
      // input_thread.join() 上一直等到用户敲一下回车或 Ctrl-D，看上去像卡死。
      // 管道/重定向输入不会走到这里：那种情况下输入线程会在下一次醒来时看到
      // active_workers == 0 主动退出。所以这里只对终端提示一句，让它不像卡死。
      input_still_reading = !dispatcher->input_finished;
    }
    dispatcher->cv_space.notify_all();
    dispatcher->cv_work.notify_all();
  }
  // 帧写出**放在锁外**：serve_frame 走的是另一把锁（g_output_mutex），在这里持着
  // dispatcher->mutex 再取它，等于凭空多出第二种锁序，没必要给自己埋雷。
  for (const auto& item : orphaned) {
    serve_err(item.rid, index, "session failed");
  }
  if (all_gone && input_still_reading && isatty(STDIN_FILENO)) {
    std::lock_guard<std::mutex> lock(g_output_mutex);
    printf("[interactive] all sessions have exited; "
           "press Enter or Ctrl-D to finish\n");
    fflush(stdout);
  }
}

// 把一条待办交给会话线程。队列满时在这里等（反压）。两个前端共用。
static PushResult dispatcher_push(InteractiveDispatcher* dispatcher, const PendingInput& item)
{
  std::unique_lock<std::mutex> lock(dispatcher->mutex);
  const bool pinned = item.target_session >= 0 &&
                      (size_t)item.target_session < dispatcher->worker_alive.size();
  auto target_dead = [&]() {
    return pinned && !dispatcher->worker_alive[item.target_session];
  };
  // 目标会话死没死要**先看**，等队列空间是后话：目标已经死了的话，等空间纯属白等
  // （钉给死会话的活没有任何人会消费它），而请求方在等一个永远不来的答案。
  if (target_dead()) {
    return PushResult::kDeadTarget;
  }
  dispatcher->cv_space.wait(lock, [dispatcher]() {
    return dispatcher->pending.size() < kInteractivePendingCap ||
           dispatcher->active_workers == 0;
  });
  if (dispatcher->active_workers == 0) {
    return PushResult::kWorkersGone;
  }
  // **醒来之后必须再判一次**（2026-09-16，主机端 t5 实测踩到）：
  // 上面那次判断只解决了"别白等"，它不能代表"等到位置时目标还活着"。队列满 + 目标
  // 会话在这段等待里死掉，是一个真实存在的窗口——而且**恰好只有目标死掉才能解开这个
  // 等待**（它退出时会把队列里钉给它的活扫掉、腾出位置）。于是醒来的这一刻，正是
  // "它已经死了"最可能成立的一刻：那条活刚被推进队列，就已经没有任何人会消费它。
  //
  // 为什么漏不掉：会话线程置 worker_alive=false 与清扫队列是**同一把锁**里的两件事，
  // 本函数从头到尾也持着这把锁。两者只能一前一后：
  //   · 本函数在前 → 上面那次判断看到 true，推进去的那条随后被对方的清扫捞走 → 有回帧；
  //   · 对方在前 → 这里再判一次看到 false → kDeadTarget → 有回帧。
  // 少了这次判断，第二种情形就会留下一条**永远没人回答**的活：网关那边表现为白等
  // REQUEST_TIMEOUT（默认 1800s），而且它占着队列额度、没有任何迹象。
  if (target_dead()) {
    return PushResult::kDeadTarget;
  }
  dispatcher->pending.push_back(item);
  // notify_all 而不是 notify_one：服务模式下队首可能正钉给某个特定会话，被唤醒的
  // 若恰好是别人，这一条活就没人取了（它会一直躺在队列里）。
  dispatcher->cv_work.notify_all();
  return PushResult::kOk;
}

// 输入线程：把 stdin 的每一行放进待办队列，满了就等。
static void run_interactive_input_worker(InteractiveDispatcher* dispatcher)
{
  std::string line;
  // 这里刻意不用 read_line_utf8：它自己逐字符回显并处理退格，而此刻 N 路回复
  // 正在往同一个 stdout 上打，回显会把行内容插进别人的回复块中间。改用规范模式下的
  // std::getline，行编辑与回显交给终端驱动，谁也干扰不到谁。
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line[line.size() - 1] == '\r') {
      line.erase(line.size() - 1);  // stdin 若是 CRLF（管道/文件）就去掉行尾 \r
    }
    if (line.empty()) {
      continue;  // 与单会话交互模式一致：空行不当作一轮输入
    }
    PendingInput item;
    item.text = std::move(line);
    // 交互模式下 target_session 恒为 -1，所以只可能是 kOk 或 kWorkersGone。
    if (dispatcher_push(dispatcher, item) != PushResult::kOk) {
      break;
    }
  }

  std::lock_guard<std::mutex> lock(dispatcher->mutex);
  dispatcher->input_eof = true;
  dispatcher->input_finished = true;
  dispatcher->cv_work.notify_all();
}

// prompt 载荷的长度上限。它**只管一件事**：别相信头行里那个数字。没有上限的话，
// 一个手滑的 2^60 会让本进程按它去 reserve 内存，直接把服务打死——这条流由网关
// 直接驱动，它一旦算出个错长度，我们不该跟着一起崩。
//
// **它不是"这一轮太长"的判据**，那条判据是 tokenizer 的（装不下就 REJECT，见会话
// 驱动循环里数 incoming_tokens 那一段）。1 MiB 远在任何 8192 位置上装得下的 prompt
// 之上——那个量级是几十 KB——所以在这里拦下来的请求，tokenizer 那条也一定会拦。
// 两者真正的区别在**拦下来之后干什么**，而这里曾经写的是 `break`。
static const size_t kServeMaxPromptBytes = 1u << 20;

// KV 存取路径的长度上限（改动 C）。路径不需要很大，4096 足够；设上限是为了和 prompt
// 一样，把"撒谎的头行"挡在读载荷之前。
static const size_t kServeMaxPathBytes = 4096;

// 每轮 max_new_tokens 的上限。协议里这个字段实际按 `int` 用（`PendingInput`、
// `run_chat_turn` 都是 int），而头行是用 `%llu` 读进 `unsigned long long` 的——
// 中间那次 `(int)` 截断以前**没有任何校验**，于是 `4294967296` 会变成 0（= "用进程
// 默认值"）、`4294967297` 变成 1、`2^64-1` 变成 -1（也走"用默认值"这条）：
// 网关把上限写成表达式、或将来加了单位换算，都会得到"看起来被接受了、实际跑的是别的
// 数字"。这里直接卡在 int 的正数范围内，宁可回一帧 ERR 讲清楚，也不静默换个数。
static const unsigned long long kServeMaxNewTokens = 0x7fffffffull;

// 把"声明了长度、但这一轮不打算处理"的载荷读掉丢弃，保持帧流对齐。
// 逐块读、不按 prompt_len 去 reserve：长度上限已经在外层卡过一道，这里的固定块只是
// 让丢弃动作本身与具体长度无关。
static void discard_serve_payload(unsigned long long prompt_len)
{
  char chunk[4096];
  unsigned long long left = prompt_len;
  while (left > 0) {
    const std::streamsize want =
        (std::streamsize)(left < sizeof(chunk) ? left : (unsigned long long)sizeof(chunk));
    std::cin.read(chunk, want);
    const std::streamsize got = std::cin.gcount();
    if (got <= 0) {
      break;  // stdin 提前结束：外层 getline 下一次就会看到 EOF 而收工
    }
    left -= (unsigned long long)got;
  }
}

// --serve 的输入线程：从 stdin 读请求帧（语法见 g_serve_mode 上方的注释）。
//
// 与交互模式的关键差别是**载荷不定长**：prompt 里可以有换行、制表符、任意字节，
// 所以不能用 getline 读完整条请求——头行定长可解析，载荷按头行里声明的长度原样读，
// 不做任何转义/反转义。网关能用中文、多行 prompt，靠的就是这个。
static void run_serve_input_worker(InteractiveDispatcher* dispatcher, int session_count,
                                   const std::vector<StageContext>& stages,
                                   Tokenizer* tokenizer)
{
  std::string header;
  while (std::getline(std::cin, header)) {
    if (!header.empty() && header[header.size() - 1] == '\r') {
      header.erase(header.size() - 1);
    }
    if (header.empty()) {
      continue;
    }
    if (header == "QUIT") {
      break;  // 网关要求收工：不必等 stdin EOF，便于优雅关闭
    }
    if (header == "STAT") {
      // 资源观测。**必须在这里就地答复、不让它进调度队列**：这一帧不需要会话，
      // 而排队的话它会被前面那条正在跑的长 decode 挡在后面——恰恰是"板卡正忙、
      // 想看它在忙什么"的时候出不来数。serve_stat 只读原子量，所以就地答复是
      // 零阻塞的（它不碰设备，也不需要任何锁）。
      serve_stat(stages);
      continue;
    }
    if (header.compare(0, 5, "STOP ") == 0) {
      // 停掉在跑的那一轮（网页上的「暂停回答」）。**就地处理、不进调度队列**——
      // 和上面 STAT 同一个理由，而且这里更绝对：排到队尾的话，它前面正好是它想停的
      // 那条长 decode（那个会话被钉着，队列就那么长），等它跑完再去停等于没停。
      //
      // 只置标志位就返回：不等待、不碰设备、不取卡锁。输入线程是整条链的咽喉，
      // 它一卡住，连 STOP 自己都再也发不进来了。
      // 用 unsigned long long 而不是 uint64_t：`%llu` 在 aarch64(LP64) 上就是
      // unsigned long long，uint64_t 却是 unsigned long，两者虽然同宽、g++ 还是会
      // 报 -Wformat。和下面 REQ / RESUME 的解析用同一个类型，零 warning。
      unsigned long long stop_rid = 0;
      if (std::sscanf(header.c_str(), "STOP %llu", &stop_rid) == 1) {
        std::lock_guard<std::mutex> lock(dispatcher->mutex);
        std::map<uint64_t, std::shared_ptr<std::atomic<bool>>>::iterator it =
            dispatcher->inflight.find(stop_rid);
        if (it != dispatcher->inflight.end() && it->second) {
          it->second->store(true);
        }
        // 找不到 = 那一轮已经跑完（或者从来不存在）：**静默无事发生**。回一帧反而
        // 多一种网关要处理的状态，而有没有停到，从 DONE 就看得出来。
      }
      continue;
    }
    if (header.compare(0, 7, "RESUME ") == 0) {
      // 继续上一轮被打断的 decode（网页「暂停」后的「继续回答」）。与 REQ 的差别只在
      // PendingInput 上的 resume 标志位：入队、登记、派发、注销走的是同一条路。
      // 它不带 prompt，所以「超长载荷 / 载荷丢字节」那套校验对它不适用，只校验头行形状
      // 与会话号；rid 不认识/会话越界同样是静默或 ERR（沿用 REQ 的语义）。
      unsigned long long r_rid = 0, r_session = 0, r_max_new = 0;
      if (std::sscanf(header.c_str(), "RESUME %llu %llu %llu",
                      &r_rid, &r_session, &r_max_new) == 3 &&
          r_session < (unsigned long long)session_count) {
        PendingInput item;
        item.rid = r_rid;
        item.target_session = (int)r_session;
        item.max_new_tokens = (int)r_max_new;
        item.resume = true;
        item.cancel = std::make_shared<std::atomic<bool>>(false);
        {
          std::lock_guard<std::mutex> lock(dispatcher->mutex);
          dispatcher->inflight[item.rid] = item.cancel;
        }
        const PushResult pushed = dispatcher_push(dispatcher, item);
        if (pushed != PushResult::kOk) {
          std::lock_guard<std::mutex> lock(dispatcher->mutex);
          dispatcher->inflight.erase(item.rid);
        }
        if (pushed == PushResult::kDeadTarget) {
          serve_err((uint64_t)r_rid, (int)r_session, "session failed");
        }
      } else {
        serve_err(0, -1, "bad resume header: " + header);
      }
      continue;
    }

    unsigned long long rid = 0, max_new = 0, reset = 0, prompt_len = 0;
    int                session = -1;
    // 精确计数（网关窗口裁剪用）：`COUNT <rid> <len>` + 载荷，就地数、就地回，不进调度
    // 队列、不占会话、不碰设备——和 STAT/STOP 同一个理由（排队会被前面的长 decode 挡住）。
    // 它带载荷，所以必须自己把字节读干净，否则帧流会错位（下面越界分支同款处理）。
    if (header.compare(0, 6, "COUNT ") == 0) {
      unsigned long long c_rid = 0, c_len = 0;
      if (std::sscanf(header.c_str(), "COUNT %llu %llu", &c_rid, &c_len) != 2) {
        serve_err(0, -1, "bad count header: " + header);
        continue;
      }
      if (c_len > kServeMaxPromptBytes) {
        discard_serve_payload(c_len);
        serve_counted(c_rid, -1);
        continue;
      }
      std::string text;
      if (c_len > 0) {
        text.resize((size_t)c_len);
        std::cin.read(&text[0], (std::streamsize)c_len);
        if ((unsigned long long)std::cin.gcount() != c_len) {
          serve_err(c_rid, -1, "short read on count payload");
          break;  // 流已错位，和 REQ 的短读同一条处理
        }
      }
      serve_counted(c_rid, count_prompt_tokens(tokenizer, text));
      continue;
    }

    // 改动 C：KV 存取（会话换页）。`SAVE <rid> <session> <path_len>` + path
    //                      `LOAD <rid> <session> <ctx_tokens> <path_len>` + path
    // 由会话自己的驱动线程执行（经过调度队列钉到目标会话），所以它天然与推理串行。
    if (header.compare(0, 5, "SAVE ") == 0 || header.compare(0, 5, "LOAD ") == 0) {
      const bool         is_load = (header[0] == 'L');
      const char*        op_tag = is_load ? "LOADED " : "SAVED ";
      unsigned long long op_rid = 0, op_session = 0, op_ctx = 0, path_len = 0;
      const int parsed = is_load
          ? std::sscanf(header.c_str(), "LOAD %llu %llu %llu %llu",
                        &op_rid, &op_session, &op_ctx, &path_len)
          : std::sscanf(header.c_str(), "SAVE %llu %llu %llu",
                        &op_rid, &op_session, &path_len);
      if ((is_load && parsed != 4) || (!is_load && parsed != 3)) {
        serve_err(0, -1, "bad kv op header: " + header);
        continue;
      }
      if (path_len > kServeMaxPathBytes) {
        discard_serve_payload(path_len);
        serve_op_result(op_tag, op_rid, 0);
        continue;
      }
      if (op_session >= (unsigned long long)session_count) {
        discard_serve_payload(path_len);
        serve_err(op_rid, (int)op_session, "session index out of range");
        continue;
      }
      std::string path;
      if (path_len > 0) {
        path.resize((size_t)path_len);
        std::cin.read(&path[0], (std::streamsize)path_len);
        if ((unsigned long long)std::cin.gcount() != path_len) {
          break;  // 流已错位
        }
      }
      PendingInput item;
      item.rid = (uint64_t)op_rid;
      item.target_session = (int)op_session;
      item.kind = is_load ? PendingInput::Kind::kLoad : PendingInput::Kind::kSave;
      item.kvcache_path = std::move(path);
      item.context_tokens_hint = (uint64_t)op_ctx;
      const PushResult pushed = dispatcher_push(dispatcher, item);
      if (pushed == PushResult::kDeadTarget) {
        serve_op_result(op_tag, op_rid, 0);   // 会话已死：当这次换页没成功
      } else if (pushed == PushResult::kWorkersGone) {
        break;
      }
      continue;
    }

    // 解析不满 5 个字段就是协议错位了。回一帧 ERR 而不是静默丢弃——静默丢弃会让
    // 网关一直等这个 rid，现场看起来像"服务卡住"，实际是它自己发错了。
    if (std::sscanf(header.c_str(), "REQ %llu %d %llu %llu %llu",
                    &rid, &session, &max_new, &reset, &prompt_len) != 5) {
      serve_err(0, -1, "bad request header: " + header);
      continue;
    }
    // 长度上限先行：它决定载荷还能不能安全跳过（见下面越界分支）。顺序不能反——
    // 反过来的话，「会话号越界 + prompt_len 声明成 2^60」会先去跳一个天文数字的载荷。
    if (prompt_len > kServeMaxPromptBytes) {
      // **把载荷丢掉接着跑，不能 break**。"载荷读不下去"只对**撒谎的头行**成立
      // （声明了长度、字节却没发出来），而写这条流的只有我们自己那个网关，它永远
      // 按 `len(payload)` 写满——所以这个分支实际收到的载荷是**真的**，和下面两个
      // 越界分支一样读得掉、丢得起。
      //
      // break 的代价是"一条请求发错了"升级成"整个服务没了"：输入线程一退，四个
      // 会话全死，网关之后每个请求都 503 直到重启，池子里排队的人等满 queue_timeout
      // 也等不到人，而 /health 还在报 sessions: 4。2026-09-17 上板实测，就是这么被
      // Claude Code 的一个 3.4 MB 请求打死的。
      //
      // 回 REJECT 而不是 ERR：会话是好的、这一轮也确实一个 token 都没跑（KV 一个
      // 字节都没动，所以**不发 CLEAR**）。网关收到 REJECT 只失败这一条请求（400）
      // 并把会话留着；ERR 会被 mark_dead，撞四次又回到"四个会话全死"。
      discard_serve_payload(prompt_len);
      serve_reject(rid, session,
                   "prompt too long: " + std::to_string(prompt_len) +
                       " bytes exceeds the frame limit of " +
                       std::to_string(kServeMaxPromptBytes) +
                       " bytes; shorten this request");
      continue;
    }
    if (session >= session_count || session < -1) {
      // 头行合法但会话号越界：**必须把载荷读掉**再回 ERR，不能直接 continue。
      // 这 prompt_len 个字节留在流里的话，下一次循环会把载荷开头的几个字节当成长度
      // 为零的头行读——从此整条帧流**永久错位**，后面每个请求都被解析成
      // bad request header（网关那边看到的现象是"每个请求都立刻失败"）。
      // 网关正常运行时到不了这里（池子大小取自后端 READY 的会话数，派发处都有范围
      // 守卫），所以这条的价值是"防线被踩时别把可恢复错误升级成永久错位"。
      discard_serve_payload(prompt_len);
      serve_err(rid, session, "session index out of range");
      continue;
    }
    if (max_new > kServeMaxNewTokens) {
      // 同上：载荷要读掉再回 ERR，别让流错位。
      discard_serve_payload(prompt_len);
      serve_err(rid, session,
                "max_new_tokens out of range: " + std::to_string(max_new) + " (must be <= " +
                    std::to_string(kServeMaxNewTokens) + ")");
      continue;
    }

    std::string prompt;
    if (prompt_len > 0) {
      prompt.resize((size_t)prompt_len);
      std::cin.read(&prompt[0], (std::streamsize)prompt_len);
      if ((unsigned long long)std::cin.gcount() != prompt_len) {
        serve_err(rid, session, "short read on prompt payload");
        break;  // 同上：流已错位
      }
    }

    PendingInput item;
    item.text = std::move(prompt);
    item.rid = (uint64_t)rid;
    item.target_session = session;
    item.max_new_tokens = (int)max_new;
    item.reset = (reset != 0);
    item.cancel = std::make_shared<std::atomic<bool>>(false);
    // 登记**必须在入队之前**：反过来写的话，会话线程可能已经取走这条活、甚至已经
    // 跑完并把它注销掉了，这里再挂一个没人认领的标志位上去，就是一条永不释放的记录
    // （网关的 rid 单调递增，不复用）。
    {
      std::lock_guard<std::mutex> lock(dispatcher->mutex);
      dispatcher->inflight[item.rid] = item.cancel;
    }
    const PushResult pushed = dispatcher_push(dispatcher, item);
    if (pushed != PushResult::kOk) {
      // 没入成队就没人会跑它、也没人会注销它——就地摘掉。上面刚登记的那一条如果
      // 留着，就是表随请求数线性长，而且没有任何症状。
      std::lock_guard<std::mutex> lock(dispatcher->mutex);
      dispatcher->inflight.erase(item.rid);
    }
    if (pushed == PushResult::kDeadTarget) {
      // 会话在网关派活之后、入队之前死掉了（推理失败）。回一帧 ERR 让网关立刻把这段
      // 对话挪到别的会话上——不回的后果是它一直等到 REQUEST_TIMEOUT（默认 1800s），
      // 期间白占着一个租约，排队的人全被挡在后面。
      // 这里用 ERR（而不是 REJECT）：会话**确实**死了，网关把它标死是对的。
      serve_err(rid, session, "session failed");
      continue;
    }
    if (pushed == PushResult::kWorkersGone) {
      break;
    }
  }

  std::lock_guard<std::mutex> lock(dispatcher->mutex);
  dispatcher->input_eof = true;
  dispatcher->input_finished = true;
  dispatcher->cv_work.notify_all();
}

// 交互式多会话的收尾统计。吞吐用与 --rounds 路径相同的「并发窗口」口径，
// 不把各会话的时间相加（并发下那会重复计时）。
static void report_interactive_sessions(
    const std::vector<std::unique_ptr<Conversation>>& convs,
    const std::vector<SessionTiming>& timings, int max_new_tokens)
{
  printf("\n=== Multi-Session Interactive Summary ===\n");
  printf(" max tokens per turn: %d\n\n", max_new_tokens);
  printf(" %-6s | %-6s | %-11s | %-10s | %-13s | %-8s\n",
         "Session", "Turns", "Prefill tok", "Decode tok", "Decode tok/s", "Status");
  printf("---------------------------------------------------------------------------\n");

  uint64_t total_decode_tokens = 0;
  for (size_t i = 0; i < convs.size(); ++i) {
    const Conversation& conv = *convs[i];
    const double decode_s = conv.total_decode_ms / 1e3;
    const double decode_tps = decode_s > 0.0 ? (double)conv.total_decode_tokens / decode_s : 0.0;
    total_decode_tokens += conv.total_decode_tokens;
    printf(" s%-5zu | %-6d | %-11llu | %-10llu | %-13.2f | %-8s\n",
           i, conv.turns_done,
           (unsigned long long)conv.total_prefill_tokens,
           (unsigned long long)conv.total_decode_tokens,
           decode_tps,
           conv.failed ? "FAILED" : "ok");
  }
  printf("---------------------------------------------------------------------------\n");

  uint64_t window_start_us = 0;
  uint64_t window_end_us = 0;
  bool     window_valid = false;
  for (const auto& t : timings) {
    if (!t.valid) {
      continue;
    }
    const uint64_t s = timeval_to_us(t.start);
    const uint64_t e = timeval_to_us(t.end);
    if (!window_valid) {
      window_start_us = s;
      window_end_us = e;
      window_valid = true;
    } else {
      if (s < window_start_us) window_start_us = s;
      if (e > window_end_us) window_end_us = e;
    }
  }
  if (window_valid && window_end_us > window_start_us) {
    const double window_s = (window_end_us - window_start_us) / 1e6;
    printf(" total decode tokens: %llu over %.3f s (first turn start -> last turn end) = %.2f tok/s\n",
           (unsigned long long)total_decode_tokens, window_s,
           (double)total_decode_tokens / window_s);
    printf(" (口径说明：窗口取各会话「第一轮开工」到「最后一轮结束」的并集，\n"
           "  不包含纯等待输入的时间；与 --rounds 路径的聚合口径同源，可直接对比)\n");
  }
}

// 并发模式的结果汇总。返回 0 表示所有会话都正常跑完。
static int report_concurrent_sessions(const std::vector<std::unique_ptr<Conversation>>& convs,
                                       const std::vector<SessionTiming>& timings,
                                       const char* prompt, int rounds, int max_new_tokens)
{
  bool all_ok = true;
  uint64_t total_decode_tokens = 0;
  uint64_t total_prefill_tokens = 0;

  printf("\n=== Multi-Session Concurrent Run ===\n");
  printf("prompt      : %.48s%s\n", prompt, strlen(prompt) > 48 ? "..." : "");
  printf("conversations: %zu, rounds: %d, max tokens/round: %d\n\n",
         convs.size(), rounds, max_new_tokens);

  printf(" %-6s | %-10s | %-10s | %-14s | %-12s | %-8s\n",
         "Session", "Prefill tok", "Decode tok", "Decode tok/s", "Wall (ms)", "Status");
  printf("---------------------------------------------------------------------------\n");

  for (size_t i = 0; i < convs.size(); ++i) {
    const Conversation& conv = *convs[i];
    const double decode_s = conv.total_decode_ms / 1e3;
    const double decode_tps = decode_s > 0.0 ? (double)conv.total_decode_tokens / decode_s : 0.0;
    total_decode_tokens += conv.total_decode_tokens;
    total_prefill_tokens += conv.total_prefill_tokens;
    if (conv.failed) {
      all_ok = false;
    }
    printf(" s%-5zu | %-10llu | %-10llu | %-14.2f | %-12.1f | %-8s\n",
           i,
           (unsigned long long)conv.total_prefill_tokens,
           (unsigned long long)conv.total_decode_tokens,
           decode_tps,
           conv.wall_ms,
           conv.failed ? "FAILED" : "ok");
  }
  printf("---------------------------------------------------------------------------\n");

  // 聚合口径：所有会话从最早起跑到最晚结束的那段墙钟（并发窗口），
  // 而不是各会话时间之和——后者在并发下会重复计时。
  uint64_t window_start_us = 0;
  uint64_t window_end_us = 0;
  bool     window_valid = false;
  for (const auto& t : timings) {
    if (!t.valid) {
      continue;
    }
    const uint64_t s = timeval_to_us(t.start);
    const uint64_t e = timeval_to_us(t.end);
    if (!window_valid) {
      window_start_us = s;
      window_end_us = e;
      window_valid = true;
    } else {
      if (s < window_start_us) window_start_us = s;
      if (e > window_end_us) window_end_us = e;
    }
  }

  if (window_valid && window_end_us > window_start_us) {
    const double window_s = (window_end_us - window_start_us) / 1e6;
    const double aggregate_tps = (double)total_decode_tokens / window_s;
    printf(" aggregate   : %llu decode tokens over %.3f s wall = %.2f tok/s",
           (unsigned long long)total_decode_tokens, window_s, aggregate_tps);
    if (convs.size() > 1) {
      // 单会话基线（80.75 ms/tok ≈ 12.38 tok/s）来自 P0 实测；这里给出相对它、
      // 以及相对「N 倍线性」的达成率，便于一眼看出串行化损失了多少。
      const double baseline_tps = 12.38;
      printf("\n               (single-session baseline 12.38 tok/s → speedup %.2fx, "
             "linear would be %.2fx, efficiency %.0f%%)",
             aggregate_tps / baseline_tps,
             (double)convs.size(),
             100.0 * (aggregate_tps / baseline_tps) / (double)convs.size());
    }
    printf("\n");
  }
  printf(" total prefill tokens: %llu\n", (unsigned long long)total_prefill_tokens);

  return all_ok ? 0 : 1;
}

// ============================================================================
// 多 Session 可行性探针（--probe-sessions N）
//
// 在写任何并发代码之前，用最小代价回答三个"一票否决"问题：
//   1) rknn3_session_init 是否为每个 session 复制一份权重？
//      每卡权重约 3.79GB，若复制则第二个 session 直接 OOM，四卡并发方案不成立。
//   2) KV cache 是 per-session 独立分配的吗？分配时机在 init 还是首次推理？
//   3) 两个 session 的 KV cache 是否真正隔离（B 的 prefill 不会污染 A 的上下文）？
//
// 做法：复用已初始化好的 N 个 stage（每卡 1 个 session），再为每卡逐步创建额外
//       session，每创建一轮测一次设备内存；最后在两组 session 上交错跑 prefill/decode
//       验证 KV 隔离性，并用"同组重跑"排除采样非确定性造成的假阴性。
//
// 探针只读/只建 session，不修改模型文件；结束时销毁额外 session 并退出。
// ============================================================================

#define PROBE_MAX_DEVICES 64

struct ProbeMemSample
{
  int      n_devices = 0;
  uint64_t free_bytes[PROBE_MAX_DEVICES] = {};
  uint64_t total_free = 0;
};

static double probe_mb(uint64_t bytes)
{
  return (double)bytes / 1048576.0;
}

static bool probe_sample_mem(ProbeMemSample* sample)
{
  rknn3_devices devs;
  memset(&devs, 0, sizeof(devs));
  if (rknn3_find_devices(&devs) != RKNN3_SUCCESS) {
    return false;
  }
  sample->n_devices = devs.n_devices < PROBE_MAX_DEVICES ? devs.n_devices : PROBE_MAX_DEVICES;
  sample->total_free = 0;
  for (int i = 0; i < sample->n_devices; ++i) {
    sample->free_bytes[i] = devs.devices[i].mem_info.sys_free;
    sample->total_free += devs.devices[i].mem_info.sys_free;
  }
  return true;
}

static void probe_print_mem(const ProbeMemSample& sample, const char* tag)
{
  printf("[probe] %s\n", tag);
  for (int i = 0; i < sample.n_devices; ++i) {
    printf("[probe]   card[%d] free = %9.1f MB\n", i, probe_mb(sample.free_bytes[i]));
  }
  printf("[probe]   TOTAL  free = %9.1f MB\n", probe_mb(sample.total_free));
}

static void probe_print_mem_diff(const ProbeMemSample& before, const ProbeMemSample& after,
                                 const char* tag)
{
  printf("[probe] %s\n", tag);
  const int n = after.n_devices < before.n_devices ? after.n_devices : before.n_devices;
  for (int i = 0; i < n; ++i) {
    double delta = probe_mb(after.free_bytes[i]) - probe_mb(before.free_bytes[i]);
    printf("[probe]   card[%d] free %9.1f -> %9.1f MB   delta %+9.1f MB\n",
           i, probe_mb(before.free_bytes[i]), probe_mb(after.free_bytes[i]), delta);
  }
  double total_delta = probe_mb(after.total_free) - probe_mb(before.total_free);
  printf("[probe]   TOTAL  free %9.1f -> %9.1f MB   delta %+9.1f MB\n",
         probe_mb(before.total_free), probe_mb(after.total_free), total_delta);
}

static void probe_dump_allocation(const StageContext& stage, const char* tag);

// 打印每个 stage 的 context 级内存：权重 / 内部 / KV cache，以及 KV cache 长度分组。
// 这些数字回答"卡内放得下几个 session"。
static void probe_dump_context_mem(const StageContext& stage)
{
  printf("\n--- [probe] %s (device=%s) ---\n", stage.name.c_str(), stage.device_id.c_str());

  rknn3_llm_config llm_cfg;
  memset(&llm_cfg, 0, sizeof(llm_cfg));
  if (rknn3_query(stage.ctx, RKNN3_QUERY_LLM_CONFIG, &llm_cfg, sizeof(llm_cfg)) == RKNN3_SUCCESS) {
    printf("[probe]   llm: vocab=%u emb_dim=%u max_ctx_len=%u max_pos_emb=%u model_type=%s\n",
           llm_cfg.vocab_size, llm_cfg.embedding_dim, llm_cfg.max_ctx_len,
           llm_cfg.max_position_embeddings,
           llm_cfg.model_type ? llm_cfg.model_type : "(null)");
    printf("[probe]   kvcache: dtype=%d store_method=%d group_size=%u residual_depth=%u\n",
           (int)llm_cfg.kvcache_dtype, (int)llm_cfg.kvcache_store_method,
           llm_cfg.kvcache_group_size, llm_cfg.kvcache_residual_depth);
    for (uint32_t a = 0; a < llm_cfg.n_attention_kvcache_lens && a < RKNN3_MAX_ATTENTION_TYPE_NUM; ++a) {
      printf("[probe]   attn[%u] type=%d n_lens=%u lens:", a,
             (int)llm_cfg.attention_kvcache_lens[a].attention_type,
             llm_cfg.attention_kvcache_lens[a].n_kvcache_buffer_lens);
      for (uint32_t l = 0; l < llm_cfg.attention_kvcache_lens[a].n_kvcache_buffer_lens &&
                         l < RKNN3_MAX_KVCACHE_LEN_GROUPS; ++l) {
        printf(" %d", llm_cfg.attention_kvcache_lens[a].kvcache_buffer_lens[l]);
      }
      printf("\n");
    }
  }

  // 设备级内存：sys_* 是本机侧的小块内存，真正要看的是每个 node 的可用量。
  rknn3_dev_mem_info dev_mem;
  memset(&dev_mem, 0, sizeof(dev_mem));
  if (rknn3_query(stage.ctx, RKNN3_QUERY_DEVICE_MEM_INFO, &dev_mem, sizeof(dev_mem)) == RKNN3_SUCCESS) {
    printf("[probe]   device: nodes=%u sys_total=%.1f MB sys_free=%.1f MB\n",
           dev_mem.node_num, probe_mb(dev_mem.sys_total), probe_mb(dev_mem.sys_free));
    uint32_t n = dev_mem.node_num < RKNN3_MAX_NPU_NODE_NUM ? dev_mem.node_num : RKNN3_MAX_NPU_NODE_NUM;
    for (uint32_t n_idx = 0; n_idx < n; ++n_idx) {
      printf("[probe]     node[%u] total=%9.1f MB free=%9.1f MB used=%9.1f MB\n", n_idx,
             probe_mb(dev_mem.node_mem_info[n_idx].total),
             probe_mb(dev_mem.node_mem_info[n_idx].free),
             probe_mb(dev_mem.node_mem_info[n_idx].total - dev_mem.node_mem_info[n_idx].free));
    }
  }

  probe_dump_allocation(stage, nullptr);
}

// 查询 context 级内存分配明细（每核 weight/internal/kvcache）。
// 在创建额外 session 前后各调一次，"kvcache 是否增长"直接回答 KV cache 是不是 per-session。
static void probe_dump_allocation(const StageContext& stage, const char* tag)
{
  int32_t core_num = 0;
  if (rknn3_query(stage.ctx, RKNN3_QUERY_CORE_NUMBER, &core_num, sizeof(core_num)) != RKNN3_SUCCESS ||
      core_num <= 0) {
    printf("[probe]   query core number failed\n");
    return;
  }
  if (tag) {
    printf("[probe]   [%s] cores=%d\n", tag, core_num);
  } else {
    printf("[probe]   cores=%d\n", core_num);
  }

  std::vector<rknn3_allocation_info> allocs((size_t)core_num);
  memset(allocs.data(), 0, sizeof(rknn3_allocation_info) * (size_t)core_num);
  if (rknn3_query(stage.ctx, RKNN3_QUERY_ALLOCATION_INFO, allocs.data(),
                  sizeof(rknn3_allocation_info) * (size_t)core_num) == RKNN3_SUCCESS) {
    double weight_total = 0.0;
    double internal_total = 0.0;
    double kvcache_total = 0.0;
    for (int c = 0; c < core_num; ++c) {
      printf("[probe]   core[%d] weight=%9.1f MB internal=%9.1f MB kvcache=%9.1f MB\n",
             allocs[c].core_id, probe_mb(allocs[c].weight_mem.size),
             probe_mb(allocs[c].internal_mem.size), probe_mb(allocs[c].kvcache_mem.size));
      weight_total += probe_mb(allocs[c].weight_mem.size);
      internal_total += probe_mb(allocs[c].internal_mem.size);
      kvcache_total += probe_mb(allocs[c].kvcache_mem.size);
    }
    printf("[probe]   SUM: weight=%.1f MB internal=%.1f MB kvcache=%.1f MB\n",
           weight_total, internal_total, kvcache_total);
  }

  std::vector<rknn3_kvcache_len_group_info> groups((size_t)core_num);
  memset(groups.data(), 0, sizeof(rknn3_kvcache_len_group_info) * (size_t)core_num);
  if (rknn3_query(stage.ctx, RKNN3_QUERY_KVCACHE_LEN_GROUP_INFO, groups.data(),
                  sizeof(rknn3_kvcache_len_group_info) * (size_t)core_num) == RKNN3_SUCCESS) {
    for (int c = 0; c < core_num; ++c) {
      printf("[probe]   core[%d] kvcache groups=%u active_group=%d", groups[c].core_id,
             groups[c].n_groups, groups[c].active_group_id);
      for (uint32_t g = 0; g < groups[c].n_groups && g < RKNN3_MAX_KVCACHE_LEN_GROUPS; ++g) {
        printf(" [g%u]=%.1fMB", g, probe_mb(groups[c].kvcache_sizes[g]));
      }
      printf("\n");
    }
  }
}

// 把 stages[] 临时切到指定的 session 组上跑一次流水线，跑完切回原组。
// 这样可以在不改动 pipeline 结构的前提下，用不同 session 组各跑一遍推理。
static bool probe_run_on_group(std::vector<StageContext>& stages, Conversation& conv,
                               const std::vector<rknn3_session*>& group, const char* prompt,
                               const std::vector<int32_t>* input_tokens, InferencePhase phase,
                               int32_t* out_token)
{
  std::vector<rknn3_session*> saved(stages.size(), nullptr);
  for (size_t i = 0; i < stages.size(); ++i) {
    saved[i] = conv.stages[i].session;
    conv.stages[i].session = group[i];
  }

  bool ok = run_pipeline_once(stages, conv, prompt, input_tokens, phase, nullptr);
  if (ok && out_token) {
    if (!get_last_stage_token(conv, out_token)) {
      *out_token = -1;
    }
  }

  for (size_t i = 0; i < stages.size(); ++i) {
    conv.stages[i].session = saved[i];
  }
  return ok;
}

static void probe_clear_group(const std::vector<rknn3_session*>& group)
{
  for (auto* session : group) {
    if (session) {
      rknn3_session_clear_kvcache(session, RKNN3_KVCACHE_CLEAR_ALL);
    }
  }
}

// 在指定 session 组上推进一步：prompt != nullptr 走 prefill，否则用上一步的 token 走 decode。
// 隔离性测试需要把 A / B 两个会话逐步交错推进，所以这一步必须能单独调用。
static bool probe_step(std::vector<StageContext>& stages, Conversation& conv,
                       const std::vector<rknn3_session*>& group, const char* prompt,
                       int32_t prev_token, int32_t* out_token)
{
  if (prompt != nullptr) {
    return probe_run_on_group(stages, conv, group, prompt, nullptr,
                              InferencePhase::PREFILL, out_token);
  }
  std::vector<int32_t> one(1, prev_token);
  return probe_run_on_group(stages, conv, group, nullptr, &one,
                            InferencePhase::DECODE, out_token);
}

static void probe_print_seq(const char* label, const std::vector<int32_t>& seq)
{
  printf("[probe]   %s (%zu tok):", label, seq.size());
  for (size_t i = 0; i < seq.size(); ++i) {
    printf(" %d", seq[i]);
  }
  printf("\n");
}

// 把 seq 推进到 steps 个 token：首步走 prefill（prompt != nullptr），其余是 greedy decode。
// 传 prompt == nullptr 表示在会话已有 KV 上继续——隔离性测试靠它做 A/B 逐步交错。
static bool probe_advance(std::vector<StageContext>& stages, Conversation& conv,
                          const std::vector<rknn3_session*>& group, const char* prompt,
                          int steps, std::vector<int32_t>* seq)
{
  if (prompt != nullptr) {
    int32_t first = -1;
    if (!probe_step(stages, conv, group, prompt, -1, &first) || first < 0) {
      return false;
    }
    seq->push_back(first);
  }
  while ((int)seq->size() < steps) {
    int32_t next = -1;
    if (seq->empty() || !probe_step(stages, conv, group, nullptr, seq->back(), &next) ||
        next < 0) {
      return false;
    }
    seq->push_back(next);
  }
  return true;
}

// 一张卡上最紧的那个 NPU node 的空闲内存（MB）。
// session 分配失败总是发生在最紧的 node 上，所以卡内天花板由它决定而非平均值。
static double probe_tightest_node_free_mb(const StageContext& stage)
{
  rknn3_dev_mem_info dev_mem;
  memset(&dev_mem, 0, sizeof(dev_mem));
  if (rknn3_query(stage.ctx, RKNN3_QUERY_DEVICE_MEM_INFO, &dev_mem, sizeof(dev_mem)) != RKNN3_SUCCESS) {
    return -1.0;
  }
  uint32_t n = dev_mem.node_num < RKNN3_MAX_NPU_NODE_NUM ? dev_mem.node_num : RKNN3_MAX_NPU_NODE_NUM;
  double tightest = -1.0;
  for (uint32_t i = 0; i < n; ++i) {
    double free_mb = probe_mb(dev_mem.node_mem_info[i].free);
    if (tightest < 0.0 || free_mb < tightest) {
      tightest = free_mb;
    }
  }
  return tightest;
}

// 整卡口径的最紧 node 空闲：跨 4 段取最小。段间权重不同（stage3 带 norm+lm_head，
// 比 stage0 多 ~172 MB），所以真正绑死卡内天花板的是全段里最紧的那个 node。
static double probe_card_tightest_free_mb(const std::vector<StageContext>& stages)
{
  double tightest = -1.0;
  for (size_t i = 0; i < stages.size(); ++i) {
    double v = probe_tightest_node_free_mb(stages[i]);
    if (v > 0.0 && (tightest < 0.0 || v < tightest)) {
      tightest = v;
    }
  }
  return tightest;
}

// 模型实际生效的上下文上限。以模型内固化的 max_ctx_len 为准——命令行 --ctx-size
// 只在不超过它时才有意义（RK1828 上超了会被静默降到这个值）。
static int probe_model_ctx_len(const StageContext& stage)
{
  rknn3_llm_config llm_cfg;
  memset(&llm_cfg, 0, sizeof(llm_cfg));
  if (rknn3_query(stage.ctx, RKNN3_QUERY_LLM_CONFIG, &llm_cfg, sizeof(llm_cfg)) != RKNN3_SUCCESS) {
    return -1;
  }
  return (int)llm_cfg.max_ctx_len;
}

// context 级内存分配汇总，用于对比"建 session 前后"的权重 / KV cache 变化。
// 这是判定权重复制与否的决定性证据：
//   delta_weight ≈ 0 且 delta_kvcache > 0  → 权重共享，KV cache per-session ✅
//   delta_weight ≈ 权重总量                → 权重被逐 session 复制       ❌
struct ProbeAllocSums
{
  double weight_mb = 0.0;
  double internal_mb = 0.0;
  double kvcache_mb = 0.0;
  bool   valid = false;
};

static bool probe_get_alloc_sums(const StageContext& stage, ProbeAllocSums* out)
{
  *out = ProbeAllocSums();
  int32_t core_num = 0;
  if (rknn3_query(stage.ctx, RKNN3_QUERY_CORE_NUMBER, &core_num, sizeof(core_num)) != RKNN3_SUCCESS ||
      core_num <= 0) {
    return false;
  }
  std::vector<rknn3_allocation_info> allocs((size_t)core_num);
  memset(allocs.data(), 0, sizeof(rknn3_allocation_info) * (size_t)core_num);
  if (rknn3_query(stage.ctx, RKNN3_QUERY_ALLOCATION_INFO, allocs.data(),
                  sizeof(rknn3_allocation_info) * (size_t)core_num) != RKNN3_SUCCESS) {
    return false;
  }
  for (int c = 0; c < core_num; ++c) {
    out->weight_mb += probe_mb(allocs[c].weight_mem.size);
    out->internal_mb += probe_mb(allocs[c].internal_mem.size);
    out->kvcache_mb += probe_mb(allocs[c].kvcache_mem.size);
  }
  out->valid = true;
  return true;
}

static int probe_sessions(std::vector<StageContext>& stages, Conversation& conv,
                          const rknn3_llm_param& session_param, const ChatTemplateSpec* tpl,
                          int n_sessions, Tokenizer* tokenizer)
{
  const size_t n_stages = stages.size();

  printf("\n");
  printf("================================================================================\n");
  printf(" 多 Session 可行性探针 (--probe-sessions %d)\n", n_sessions);
  printf("--------------------------------------------------------------------------------\n");
  printf(" 回答：(1) 权重是否 per-session 复制  (2) KV cache 分配时机与容量\n");
  printf("       (3) 两组 session 的 KV cache 是否真正隔离\n");
  printf("================================================================================\n");

  ProbeMemSample baseline;
  if (!probe_sample_mem(&baseline)) {
    printf("[probe] ERROR: rknn3_find_devices failed\n");
    return -1;
  }
  printf("\n");
  probe_print_mem(baseline, "基线设备内存（每卡 1 个 session，模型已加载）:");

  for (const auto& stage : stages) {
    probe_dump_context_mem(stage);
  }

  // 采集"只有 1 个 session"时的 context 内存分配，作为判据基线。
  std::vector<ProbeAllocSums> alloc_before(n_stages);
  for (size_t i = 0; i < n_stages; ++i) {
    probe_get_alloc_sums(stages[i], &alloc_before[i]);
  }

  // ---------------- 逐步创建额外 session，每轮测一次内存 ----------------------
  // groups[k][stage] = 第 k+1 组 session；groups[0] 是 init_stage 创建的原组。
  std::vector<std::vector<rknn3_session*>> groups;
  groups.push_back(std::vector<rknn3_session*>(n_stages, nullptr));
  for (size_t i = 0; i < n_stages; ++i) {
    groups[0][i] = conv.stages[i].session;
  }

  ProbeMemSample prev = baseline;
  bool all_created = true;
  // 每卡实际能容纳的 session 数。以「最后一次全部 stage 都建成功的那一组」为准：
  // RK1828 上查询接口不更新，卡内天花板只能靠这个计数反推。
  int per_card_ok = 1;
  // 建任何额外 session 之前的空闲预算——必须在这时候取，建完再取就是「已扣除」后的
  // 残值，拿它算每 session 成本会系统性偏大。
  const double free_budget_mb = probe_card_tightest_free_mb(stages);
  for (int k = 1; k < n_sessions && all_created; ++k) {
    printf("\n[probe] >>> 创建第 %d 组 session（每卡第 %d 个）...\n", k + 1, k + 1);
    std::vector<rknn3_session*> this_group(n_stages, nullptr);
    timeval group_start;
    timeval group_end;
    gettimeofday(&group_start, NULL);
    for (size_t i = 0; i < n_stages; ++i) {
      rknn3_llm_param param = session_param;
      timeval init_start;
      timeval init_end;
      gettimeofday(&init_start, NULL);
      rknn3_session* session = rknn3_session_init(stages[i].ctx, &param, 1);
      gettimeofday(&init_end, NULL);
      if (!session) {
        printf("[probe]   *** %s: rknn3_session_init FAILED（第 %d 个 session）***\n",
               stages[i].name.c_str(), k + 1);
        all_created = false;
        break;
      }
      // callback 是 per-session 的，但内容可以复用；额外 session 也装上同一套回调，
      // 这样它们才能参与真实推理。
      if (conv.stages[i].has_callback) {
        rknn3_session_set_callback(session, &conv.stages[i].callback);
      }
      rknn3_session_set_chat_template(session, "", "", "");
      this_group[i] = session;
      printf("[probe]   %s: session #%d created in %.1f ms\n",
             stages[i].name.c_str(), k + 1, elapsed_us(init_start, init_end) / 1e3);
    }
    gettimeofday(&group_end, NULL);
    if (all_created) {
      per_card_ok = k + 1;
      // 权重若被逐 session 复制，每卡要多搬 3.6GB 过 PCIe，耗时是秒级甚至十秒级；
      // 毫秒级说明只是建了个轻量会话对象。
      printf("[probe]   本轮 4 卡建 session 共耗时 %.1f ms\n", elapsed_us(group_start, group_end) / 1e3);
    }
    groups.push_back(this_group);
    if (!all_created) {
      break;
    }

    // 关键判据：新建 session 后 context 的内存分配有没有变化。
    // kvcache 增长 → KV cache 是 per-session；weight 增长 → 权重被复制。
    probe_dump_allocation(stages.front(), "after extra session");
    if (stages.size() > 1) {
      probe_dump_allocation(stages.back(), "after extra session");
    }

    ProbeMemSample now;
    if (probe_sample_mem(&now)) {
      char tag[160];
      snprintf(tag, sizeof(tag), "本轮（第 %d 个 session）内存变化:", k + 1);
      probe_print_mem_diff(prev, now, tag);
      prev = now;
    }
  }

  // ---------------- 卡内天花板与每 session 成本 --------------------------------
  // RK1828 上 rknn3_query 的分配明细不会随建 session 更新（恒 +0.0），
  // rknn3_find_devices 的 mem_info 又返回 0——两个接口都不反映 session 级分配。
  // 唯一可用的判据是「抬高 N 直到某一张卡建不出来」，用 OOM 点反推成本。
  printf("\n[probe] >>> 卡内天花板（靠抬高 N 到 OOM 反推，分配明细接口在本平台不更新）\n");
  // 注意量纲：每个 session 在卡内**每个 node 上都会分配**（各 core 持有自己那几层的
  // KV），而绑死天花板的是最紧的那个 node。所以下面是「每 session 每 node」，
  // 乘 node_num 才是「每 session 每卡」。
  uint32_t node_num = 1;
  {
    rknn3_dev_mem_info dev_mem;
    memset(&dev_mem, 0, sizeof(dev_mem));
    if (rknn3_query(stages.front().ctx, RKNN3_QUERY_DEVICE_MEM_INFO, &dev_mem,
                    sizeof(dev_mem)) == RKNN3_SUCCESS && dev_mem.node_num > 0) {
      node_num = dev_mem.node_num;
    }
  }
  double tightest_now = probe_card_tightest_free_mb(stages);
  const int extra_ok = per_card_ok - 1;   // 已成功创建的额外 session 数

  if (free_budget_mb > 0.0) {
    printf("[probe]   最紧 node 空闲：基线 %.1f MB → 建完 %d 个额外 session 后 %.1f MB，node 数 %u\n",
           free_budget_mb, extra_ok, tightest_now, node_num);
  }
  if (extra_ok > 0 && free_budget_mb > 0.0 && tightest_now > 0.0) {
    // 直接实测：建 session 前后的差值就是这批 session 真实吃掉的内存。
    double measured_node = (free_budget_mb - tightest_now) / extra_ok;
    printf("[probe]   每 session 实测消耗 ≈ %.2f MB/node = %.1f MB/卡（%d 个额外 session 的差值）\n",
           measured_node, measured_node * node_num, extra_ok);
    if (alloc_before[0].valid && alloc_before[0].kvcache_mb > 0.0) {
      double reported_card = alloc_before[0].kvcache_mb;
      printf("[probe]   对照：分配明细上报的 KV cache = %.1f MB/卡", reported_card);
      if (measured_node * node_num <= reported_card * 1.25) {
        printf(" —— 与实测同量级，口径一致 ✅\n");
      } else {
        printf(" —— 明显低于实测，说明还有未上报的 per-session 开销\n");
      }
    }
  }
  if (extra_ok > 0 && free_budget_mb > 0.0 && tightest_now > 0.0) {
    // 剩余可容纳的会话数——只在「天花板由内存决定」时才成立。下面 if/else 会明确
    // 这个前提是否满足，若满足则这是可直接用于容量规划的数字。
    double measured_node = (free_budget_mb - tightest_now) / extra_ok;
    if (measured_node > 0.01) {
      printf("[probe]   若天花板由 node 内存决定，余量 %.1f MB/node 可再容纳约 %d 个 session\n",
             tightest_now, (int)(tightest_now / measured_node));
    }
  }
  if (all_created) {
    printf("[probe]   每卡 %d 个 session 全部建成功 —— 未触及天花板，需继续抬高 N 才能定界\n",
           per_card_ok);
  } else {
    printf("[probe]   每卡天花板 = %d 个 session（第 %d 个建不出来）\n",
           per_card_ok, per_card_ok + 1);
    if (free_budget_mb > 0.0 && tightest_now > 0.0 && tightest_now > free_budget_mb / 4.0) {
      // 重要：若失败时仍剩大块空闲，说明天花板不由这个 node 的空闲量决定，
      // 账算不平——那就不能把「每 session 成本」当成容量模型来用。
      printf("[probe]   ⚠️ 注意：失败时最紧 node 仍剩 %.1f MB（占基线 %.0f%%），\n"
             "           远大于单会话成本，说明 5 这个上限**不由 node 空闲量解释**，\n"
             "           而是别的池子或硬上限——容量规划不能按「剩余/单会话成本」估算\n",
             tightest_now, tightest_now * 100.0 / free_budget_mb);
    }
    if (per_card_ok < 2) {
      printf("[probe]   *** 每卡放不下第 2 个 session —— 多会话方案不成立 ***\n");
    }
  }

  // ---------------- 权重是否被复制：判定 -------------------------------
  ProbeMemSample after_init = prev;
  printf("\n");
  probe_print_mem_diff(baseline, after_init,
                       "创建全部额外 session 后的设备内存变化（恒为 0 表示该接口不可用）:");

  // RK1828 上 rknn3_find_devices 的 mem_info 返回 0，设备内存这条路走不通。
  // 改用 context 级分配明细做判定：建 session 前后对比 weight / kvcache 增量。
  printf("\n[probe] 判定依据：context 内存分配明细（建 session 前 -> 后）\n");
  std::vector<ProbeAllocSums> alloc_after(n_stages);
  bool alloc_ok = true;
  for (size_t i = 0; i < n_stages; ++i) {
    probe_get_alloc_sums(stages[i], &alloc_after[i]);
    if (!alloc_before[i].valid || !alloc_after[i].valid) {
      alloc_ok = false;
      continue;
    }
    printf("[probe]   %s: weight %.1f -> %.1f MB (%+.1f)  "
           "internal %.1f -> %.1f MB (%+.1f)  kvcache %.1f -> %.1f MB (%+.1f)\n",
           stages[i].name.c_str(),
           alloc_before[i].weight_mb, alloc_after[i].weight_mb,
           alloc_after[i].weight_mb - alloc_before[i].weight_mb,
           alloc_before[i].internal_mb, alloc_after[i].internal_mb,
           alloc_after[i].internal_mb - alloc_before[i].internal_mb,
           alloc_before[i].kvcache_mb, alloc_after[i].kvcache_mb,
           alloc_after[i].kvcache_mb - alloc_before[i].kvcache_mb);
  }

  double weight_delta_max = 0.0;
  double kvcache_delta_min = 1e30;
  double kvcache_delta_max = -1e30;
  for (size_t i = 0; i < n_stages; ++i) {
    if (!alloc_before[i].valid || !alloc_after[i].valid) {
      continue;
    }
    double dw = alloc_after[i].weight_mb - alloc_before[i].weight_mb;
    double dk = alloc_after[i].kvcache_mb - alloc_before[i].kvcache_mb;
    if (dw > weight_delta_max) weight_delta_max = dw;
    if (dk < kvcache_delta_min) kvcache_delta_min = dk;
    if (dk > kvcache_delta_max) kvcache_delta_max = dk;
  }

  printf("\n");
  if (!all_created) {
    printf("[probe] ==> session 未全部创建，判定不可靠\n");
  } else if (!alloc_ok) {
    printf("[probe] ==> 分配明细查询失败，无法判定\n");
  } else if (weight_delta_max < 1.0 && kvcache_delta_max > 1.0) {
    printf("[probe] ==> 判定：权重【共享】✅（每卡权重增量 %.1f MB ≈ 0），\n"
           "           KV cache 按 session 增长，每卡合计增量 %.1f MB（%d 个额外 session）\n",
           weight_delta_max, kvcache_delta_max, n_sessions - 1);
  } else if (weight_delta_max >= 1.0) {
    printf("[probe] ==> 判定：权重【被复制】❌ 每卡权重增量 %.1f MB，\n"
           "           四卡并发方案在当前模型切分下不可行\n", weight_delta_max);
  } else {
    printf("[probe] ==> 判定：权重与 KV cache 均无增长（weight %+.1f MB, kvcache %+.1f MB），\n"
           "           说明该接口不反映 session 级分配，需改用实测 OOM 点判定\n",
           weight_delta_max, kvcache_delta_max);
  }

  // ---------------- KV cache 隔离性测试 ---------------------------------------
  // 单步 decode 判别力不足：两块 KV 即便真被共享，B 的 prefill 也只覆盖它自己写过的
  // 位置，一步 decode 未必分叉。这里改成 A / B 各跑「prefill + kIsolationSteps 步
  // decode」，**逐步交错**推进，再与各自单独跑的序列逐 token 比对：
  //   交错序列 == 单独序列   → 两块 KV 内容互不影响（隔离）✅
  //   单独序列两遍都不一致   → 采样本身非确定，本轮结论作废（不可采信，也不能据此判失败）
  const int kIsolationSteps = 12;
  std::vector<int32_t> seqA_i, seqB_i;    // 交错跑
  std::vector<int32_t> seqA_r, seqB_r;    // 单独跑（对照）
  std::vector<int32_t> seqA_r2, seqB_r2;  // 对照重跑（自洽性）
  bool kv_tested = false;
  bool kv_deterministic = false;
  bool kv_isolated = false;
  // 隔离性只需要两组可用的 session，不要求全部建成——所以这里判 per_card_ok 而非
  // all_created。否则一旦抬高 N 触及天花板（那正是最该同时测隔离的场景），隔离测试就被跳过。
  if (per_card_ok >= 2 && tpl != nullptr) {
    kv_tested = true;
    printf("\n[probe] >>> KV cache 隔离性测试（%d 步 decode，A/B 逐步交错）\n", kIsolationSteps);
    const std::string prompt_a = std::string(tpl->system_prompt) + tpl->user_prefix +
                                 "What is 1+1?" + tpl->user_postfix;
    const std::string prompt_b = std::string(tpl->system_prompt) + tpl->user_prefix +
                                 "What is the capital of France?" + tpl->user_postfix;

    // 1) 交错推进：A prefill → B prefill → (A decode → B decode) × (steps-1)
    probe_clear_group(groups[0]);
    probe_clear_group(groups[1]);
    bool io_ok = probe_advance(stages, conv, groups[0], prompt_a.c_str(), 1, &seqA_i) &&
                 probe_advance(stages, conv, groups[1], prompt_b.c_str(), 1, &seqB_i);
    for (int s = 1; io_ok && s < kIsolationSteps; ++s) {
      io_ok = probe_advance(stages, conv, groups[0], nullptr, s + 1, &seqA_i) &&
              probe_advance(stages, conv, groups[1], nullptr, s + 1, &seqB_i);
    }

    // 2) 对照：A / B 各自单独跑同样步数（无交错干扰）
    probe_clear_group(groups[0]);
    probe_clear_group(groups[1]);
    bool r_ok = probe_advance(stages, conv, groups[0], prompt_a.c_str(),
                              kIsolationSteps, &seqA_r) &&
                probe_advance(stages, conv, groups[1], prompt_b.c_str(),
                              kIsolationSteps, &seqB_r);

    // 3) 自洽性：对照再跑一遍，排除采样非确定性
    probe_clear_group(groups[0]);
    probe_clear_group(groups[1]);
    bool r2_ok = probe_advance(stages, conv, groups[0], prompt_a.c_str(),
                               kIsolationSteps, &seqA_r2) &&
                 probe_advance(stages, conv, groups[1], prompt_b.c_str(),
                               kIsolationSteps, &seqB_r2);

    if (!io_ok || !r_ok || !r2_ok) {
      printf("[probe]   *** 序列推进失败（io=%d ref=%d ref2=%d），隔离性未验证 ***\n",
             (int)io_ok, (int)r_ok, (int)r2_ok);
      kv_tested = false;
    } else {
      probe_print_seq("A(interleaved)", seqA_i);
      probe_print_seq("A(ref)        ", seqA_r);
      probe_print_seq("A(ref x2)     ", seqA_r2);
      probe_print_seq("B(interleaved)", seqB_i);
      probe_print_seq("B(ref)        ", seqB_r);
      probe_print_seq("B(ref x2)     ", seqB_r2);
      kv_deterministic = (seqA_r == seqA_r2) && (seqB_r == seqB_r2);
      kv_isolated = kv_deterministic && (seqA_i == seqA_r) && (seqB_i == seqB_r);
      if (!kv_deterministic) {
        printf("[probe]   对照重跑不一致 → 采样非确定，本轮隔离性结论作废\n");
      }
    }
  }

  // ---------------- 满上下文容量测试（方案的真正闸门）--------------------------
  // 上面的隔离性测试每个会话只有几十个 token，而方案要的是「4 个会话各跑满 4096」。
  // KV cache 在多数 runtime 里随上下文增长分配，所以必须让每个会话真的把上下文
  // 填满，才能回答「4 路并存放不放得下」。这是唯一能证伪方案目标的一步。
  const int kLongCtxTarget = 4;   // 方案的目标并发数
  bool longctx_tested = false;
  int  longctx_ok = 0;
  int  longctx_n = 0;
  int  longctx_tokens = -1;   // 满上下文测试实际填入的 token 数（-1 表示未知）
  if (per_card_ok >= 2 && tpl != nullptr) {
    longctx_tested = true;
    longctx_n = std::min(kLongCtxTarget, per_card_ok);

    // 以模型内固化的 max_ctx_len 为目标把 prompt 填到 ~95%，并报出真实 token 数。
    const int ctx_len = probe_model_ctx_len(stages.front());
    const int target_tokens = ctx_len > 0 ? (int)(ctx_len * 0.95) : 3900;
    printf("\n[probe] >>> 满上下文容量测试（目标：%d 个会话各填 ~%d token，模型上限 %d）\n",
           longctx_n, target_tokens, ctx_len);

    const char* para = "The quick brown fox jumps over the lazy dog near the river bank. "
                       "Pack my box with five dozen liquor jugs before the storm arrives. "
                       "How vexingly quick daft zebras jump over the sleeping guard. "
                       "Sphinx of black quartz, judge my vow and then report the tally. ";
    const std::string prefix = std::string(tpl->system_prompt) + tpl->user_prefix;
    const std::string postfix = tpl->user_postfix;

    // 按实测 token 数迭代收放，避免靠字符数瞎估。
    std::string long_prompt;
    int n_prompt_tokens = -1;
    int chars_target = target_tokens * 4;   // 起始猜测，下面用真实 tokenize 校正
    for (int iter = 0; iter < 10; ++iter) {
      std::string filler;
      while ((int)filler.size() < chars_target) {
        filler += para;
      }
      long_prompt = prefix + filler + postfix;
      int n = count_prompt_tokens(tokenizer, long_prompt);
      if (n < 0) {
        printf("[probe]   tokenizer 不可用，无法确认测试强度（跳过满上下文测试）\n");
        longctx_tested = false;
        break;
      }
      if (n > ctx_len) {
        chars_target = chars_target * ctx_len / n * 95 / 100;
        continue;
      }
      n_prompt_tokens = n;
      if (n < target_tokens - 64) {
        chars_target = chars_target * target_tokens / n;
        continue;
      }
      break;
    }
    if (longctx_tested && n_prompt_tokens > 0) {
      longctx_tokens = n_prompt_tokens;
      printf("[probe]   prompt 实测 %d token / %d 字符（占模型上限 %d 的 %.1f%%）\n",
             n_prompt_tokens, (int)long_prompt.size(), ctx_len,
             n_prompt_tokens * 100.0 / (ctx_len > 0 ? ctx_len : 1));

      // 关键：逐个会话依次填满，让占用**累加**——这才是 N 路并存的真实内存图景。
      double free_before = probe_card_tightest_free_mb(stages);
      if (free_before > 0.0) {
        printf("[probe]   起始最紧 node 空闲 = %.1f MB（%d 个会话，均为空上下文）\n",
               free_before, longctx_n);
      }
      for (int k = 0; k < longctx_n; ++k) {
        probe_clear_group(groups[k]);
        int32_t tok = -1;
        bool ok = probe_step(stages, conv, groups[k], long_prompt.c_str(), -1, &tok);
        if (!ok || tok < 0) {
          printf("[probe]   会话 #%d: 满上下文 prefill 失败 ❌  最紧 node 空闲 %.1f MB\n",
                 k + 1, probe_card_tightest_free_mb(stages));
          break;
        }
        int32_t next = -1;
        bool ok2 = probe_step(stages, conv, groups[k], nullptr, tok, &next);
        printf("[probe]   会话 #%d: prefill ok(token %d) → decode %s(token %d)  "
               "最紧 node 空闲 %.1f MB\n",
               k + 1, tok, ok2 ? "ok" : "FAIL", next,
               probe_card_tightest_free_mb(stages));
        if (ok2) {
          ++longctx_ok;
        } else {
          break;
        }
      }
      double free_after = probe_card_tightest_free_mb(stages);
      if (free_before > 0.0 && free_after > 0.0) {
        printf("[probe]   %d 个满上下文会话（各 %d token）共消耗最紧 node %.1f MB\n",
               longctx_ok, n_prompt_tokens, free_before - free_after);
        if (free_before - free_after < 0.5) {
          // 这个 0 很关键：说明 KV 在 session_init 时已按满上下文预分配，
          // 与实际用了多少上下文无关——那么容量规划只需按会话数算，与上下文长度解耦。
          printf("[probe]   ==> 填入 %d token 上下文未产生任何新增分配\n", n_prompt_tokens);
          printf("[probe]       结论：KV cache 在 session_init 时按满上下文一次性预分配，\n"
                 "       与会话实际用掉多少上下文无关 —— 容量只与「会话数」有关\n");
        } else {
          double per_session_node = (free_before - free_after) / longctx_ok;
          printf("[probe]   ==> KV 随上下文增长，满上下文下单会话 ≈ %.1f MB/node = %.1f MB/卡\n",
                 per_session_node, per_session_node * node_num);
        }
      }
    }
  }

  // ---------------- 销毁额外 session，确认内存归还 -----------------------------
  printf("\n[probe] >>> 销毁额外 session 并测量内存归还...\n");
  for (size_t k = 1; k < groups.size(); ++k) {
    for (size_t i = 0; i < groups[k].size(); ++i) {
      if (groups[k][i]) {
        rknn3_session_destroy(groups[k][i]);
        groups[k][i] = nullptr;
      }
    }
  }
  ProbeMemSample after_destroy;
  if (probe_sample_mem(&after_destroy)) {
    probe_print_mem_diff(after_init, after_destroy, "销毁额外 session 后的内存变化:");
    double leak = probe_mb(after_destroy.total_free) - probe_mb(baseline.total_free);
    printf("[probe] 与基线相比残留差值 = %+.1f MB（接近 0 说明销毁能完整归还内存）\n", leak);
  }

  // ---------------- 总结 -------------------------------------------------------
  printf("\n");
  printf("================================================================================\n");
  printf(" 探针结论\n");
  printf("================================================================================\n");
  printf(" [1] N=%d 组 session 是否全部创建成功 : %s\n", n_sessions,
         all_created ? "是 ✅" : "否 ❌（卡内内存不足）");
  printf(" [2] 每卡 session 天花板             : %d 个%s\n", per_card_ok,
         all_created ? "（未触及上限，需抬高 N 再测）" : "（实测 OOM 点）");
  printf(" [3] 权重是否 per-session 复制       : 见上方'卡内天花板'一节\n");
  printf(" [4] KV cache 隔离性                 : ");
  if (!kv_tested) {
    printf("未验证（session 未全部创建，或序列推进失败）\n");
  } else if (!kv_deterministic) {
    printf("无法判定 ⚠️ 同一会话重跑序列都不一致，采样非确定，隔离性结论不可信\n");
  } else if (kv_isolated) {
    printf("隔离正常 ✅ %d 步交错 decode 与各自单独跑的序列逐 token 一致\n",
           kIsolationSteps);
    printf("       A: %zu tok, B: %zu tok 全部吻合\n", seqA_i.size(), seqB_i.size());
  } else {
    printf("不隔离或受干扰 ❌ 交错序列与对照不符\n");
    probe_print_seq("A(interleaved)", seqA_i);
    probe_print_seq("A(ref)        ", seqA_r);
    probe_print_seq("B(interleaved)", seqB_i);
    probe_print_seq("B(ref)        ", seqB_r);
  }
  printf(" [5] 满上下文并发（方案目标）        : ");
  if (!longctx_tested) {
    printf("未测试\n");
  } else if (longctx_tokens <= 0) {
    printf("未测成（tokenizer 不可用，无法确认测试强度）\n");
  } else if (longctx_ok >= kLongCtxTarget) {
    printf("%d 路 × %d token 上下文各 prefill+decode 全部成功 ✅ —— 方案目标容量成立\n",
           longctx_ok, longctx_tokens);
  } else if (longctx_ok > 0) {
    printf("仅 %d/%d 路 × %d token 成功 ❌ —— 达不到方案的 %d 路目标，需降 ctx 或降并发\n",
           longctx_ok, kLongCtxTarget, longctx_tokens, kLongCtxTarget);
  } else {
    printf("满上下文 prefill 直接失败 ❌\n");
  }
  printf("================================================================================\n");
  return all_created ? 0 : -1;
}

// 返回字符串 s 中从 start 开始的一个 UTF-8 字符的终端显示列宽（中文等宽字符为 2，ASCII 为 1）。
static int utf8_char_width(const std::string& s, size_t start)
{
  if (start >= s.size()) {
    return 1;
  }
  mbstate_t state;
  memset(&state, 0, sizeof(state));
  wchar_t wc = 0;
  size_t n = mbrtowc(&wc, s.data() + start, s.size() - start, &state);
  if (n == (size_t)-1 || n == (size_t)-2) {
    return 1;
  }
  int w = wcwidth(wc);
  return w < 0 ? 1 : w;
}

// 从 stdin 读取一行，支持 UTF-8 多字节字符的正确退格删除。
// 仅当 stdin 是终端时启用 raw 模式；管道/重定向时回退到 std::getline。
// 返回 true 表示读到一行；false 表示 EOF（Ctrl-D 空行或输入流结束）。
static bool read_line_utf8(std::string& line)
{
  if (!isatty(STDIN_FILENO)) {
    return static_cast<bool>(std::getline(std::cin, line));
  }

  struct termios old_tio;
  struct termios new_tio;
  if (tcgetattr(STDIN_FILENO, &old_tio) != 0) {
    return static_cast<bool>(std::getline(std::cin, line));
  }
  new_tio = old_tio;
  new_tio.c_lflag &= ~(tcflag_t)(ICANON | ECHO | ISIG);
  new_tio.c_cc[VMIN] = 1;
  new_tio.c_cc[VTIME] = 0;
  if (tcsetattr(STDIN_FILENO, TCSANOW, &new_tio) != 0) {
    return static_cast<bool>(std::getline(std::cin, line));
  }

  line.clear();
  char ch;
  ssize_t n;
  bool eof = false;
  while ((n = read(STDIN_FILENO, &ch, 1)) > 0) {
    unsigned char c = (unsigned char)ch;
    if (c == '\n' || c == '\r') {
      printf("\n");
      fflush(stdout);
      break;
    }
    if (c == 0x03 || (c == 0x04 && line.empty())) {
      // Ctrl-C，或空行时的 Ctrl-D：当作 EOF
      eof = true;
      printf("\n");
      fflush(stdout);
      break;
    }
    if (c == 0x7f || c == 0x08) {
      // 退格：删除最后一个完整 UTF-8 字符（跳过续字节 0x80-0xBF）
      if (!line.empty()) {
        size_t pos = line.size() - 1;
        while (pos > 0 && ((unsigned char)line[pos] & 0xC0) == 0x80) {
          --pos;
        }
        int w = utf8_char_width(line, pos);
        line.erase(pos);
        for (int i = 0; i < w; ++i) printf("\b");
        for (int i = 0; i < w; ++i) printf(" ");
        for (int i = 0; i < w; ++i) printf("\b");
        fflush(stdout);
      }
      continue;
    }
    line.push_back(ch);
    printf("%c", ch);
    fflush(stdout);
  }

  tcsetattr(STDIN_FILENO, TCSANOW, &old_tio);
  return !eof;
}

int main(int argc, char** argv)
{
  // 让 wcwidth/mbrtowc 按当前 locale（UTF-8）正确计算宽字符列宽。
  setlocale(LC_ALL, "");

  if (argc == 2 && strcmp(argv[1], "--help") == 0) {
    print_usage(argv[0]);
    return 0;
  }

  CommandLineOptions options;
  if (!parse_command_line(argc, argv, &options)) {
    print_usage(argv[0]);
    return -1;
  }

  const char* base_model_path = options.stage0_model;
  const char* base_weight_path = options.stage0_weight;
  const char* tokenizer_path = options.tokenizer;
  const char* embedding_path = options.embedding;
  int32_t max_context_len = options.max_context_len;
  uint32_t run_core_mask = options.run_core_mask;
  g_stage_count = options.stage_count;
  g_bucket_size = options.bucket_size;
  g_interactive = options.interactive;
  g_serve_mode = options.serve;
  g_performance_mode = options.performance_mode;

  uint64_t performance_input_length = options.performance_input_length;
  uint64_t performance_output_length = options.performance_output_length;
  const char* prompt_arg = options.prompt;
  std::string prompt_buf;
  const char* prompt = nullptr;
  if (prompt_arg) {
    // 如果以 .txt 结尾，读取文件内容作为 prompt
    size_t arg_len = strlen(prompt_arg);
    if (arg_len >= 4 && strcmp(prompt_arg + arg_len - 4, ".txt") == 0) {
      int fd = open(prompt_arg, O_RDONLY);
      if (fd < 0) {
        printf("Failed to open prompt file: %s\n", prompt_arg);
        return -1;
      }
      struct stat st;
      if (fstat(fd, &st) != 0) {
        printf("Failed to stat prompt file: %s\n", prompt_arg);
        close(fd);
        return -1;
      }
      prompt_buf.resize(st.st_size);
      ssize_t n = read(fd, &prompt_buf[0], st.st_size);
      close(fd);
      if (n != st.st_size) {
        printf("Failed to read prompt file: %s\n", prompt_arg);
        return -1;
      }
      prompt = prompt_buf.c_str();
      printf("Loaded prompt from file: %s (%lld bytes)\n", prompt_arg, (long long)st.st_size);
    } else {
      prompt = prompt_arg;
    }
  }
  // 未指定 --prompt 时的默认 prompt 推迟到下面确定 chat 模板之后再拼（见 default_prompt_buf）。
  // 原实现在这里硬编码了 Qwen 格式的 prompt，对 Gemma-4 不适用。
  int max_new_tokens = options.max_new_tokens;
  g_verbose = options.verbose;
  g_ignore_eos = options.ignore_eos;
  const char* rope_path = options.rope_path;

  if (options.tensor_dump_dir && !configure_tensor_dump(options.tensor_dump_dir)) {
    return -1;
  }

  // 并发模式改用每个会话自己的 dump 文件（见下面的 --sessions 分支），
  // 这里不能也把全局文件打开——否则 N=1 时同一个路径会被打开两次、留下一份空文件。
  if (options.dump_tokens && options.dump_tokens[0] != '\0' && !options.has_sessions) {
    g_token_dump = fopen(options.dump_tokens, "w");
    if (!g_token_dump) {
      printf("failed to open --dump-tokens file: %s\n", options.dump_tokens);
      return -1;
    }
    printf("[dump] token ids -> %s\n", options.dump_tokens);
  }

  std::vector<std::string> ext_device_ids = options.device_ids;

  if (g_stage_count < 1) {
    printf("stage_count must be >= 1, got %zu\n", g_stage_count);
    return -1;
  }
  if (max_context_len <= 0) {
    printf("max_context_len must be > 0, got %d\n", max_context_len);
    return -1;
  }
  if (g_performance_mode) {
    uint64_t max_context_tokens = (uint64_t)max_context_len;
    uint64_t decode_input_tokens = performance_output_length - 1;
    if (performance_input_length > max_context_tokens ||
        decode_input_tokens > max_context_tokens - performance_input_length) {
      printf("performance lengths exceed max_context_len: input=%llu, output=%llu, max_context_len=%d\n",
             (unsigned long long)performance_input_length,
             (unsigned long long)performance_output_length,
             max_context_len);
      return -1;
    }
  }

  // 工具: 将 "model_seg0.rknn" 替换为 "model_segN.rknn"
  auto replace_seg_suffix = [](const std::string& base, size_t seg_idx) -> std::string {
    // 找到最后一个 _seg 出现的位置
    size_t pos = base.rfind("_seg");
    if (pos == std::string::npos) {
      // 没有 _seg 后缀，直接在 .rknn 前插入 _segN
      size_t dot = base.rfind(".rknn");
      if (dot != std::string::npos) {
        return base.substr(0, dot) + "_seg" + std::to_string(seg_idx) + ".rknn";
      }
      return base + "_seg" + std::to_string(seg_idx);
    }
    // 有 _seg 后缀，替换其中的数字
    size_t num_start = pos + 4; // 跳过 "_seg"
    size_t num_end = num_start;
    while (num_end < base.size() && isdigit(base[num_end])) {
      ++num_end;
    }
    return base.substr(0, num_start) + std::to_string(seg_idx) + base.substr(num_end);
  };

  // 生成所有段的模型 & 权重路径
  std::vector<std::string> model_paths(g_stage_count);
  std::vector<std::string> weight_paths(g_stage_count);
  for (size_t i = 0; i < g_stage_count; ++i) {
    model_paths[i] = replace_seg_suffix(base_model_path, i);
    weight_paths[i] = replace_seg_suffix(base_weight_path, i);
  }

  // 校验所有段文件存在
  bool all_exist = true;
  for (size_t i = 0; i < g_stage_count; ++i) {
    struct stat st;
    if (stat(model_paths[i].c_str(), &st) != 0) {
      printf("ERROR: model file not found: %s\n", model_paths[i].c_str());
      all_exist = false;
    }
    if (stat(weight_paths[i].c_str(), &st) != 0) {
      printf("ERROR: weight file not found: %s\n", weight_paths[i].c_str());
      all_exist = false;
    }
  }
  if (!all_exist) {
    return -1;
  }

  std::vector<StageContext> stages(g_stage_count);
  for (size_t i = 0; i < stages.size(); ++i) {
    stages[i].name = "stage" + std::to_string(i);
  }
  // 单会话路径用 conversation；--sessions N 时改用 multi_conversations 里的 N 个，
  // conversation 本身不建 session（保持为默认状态，随后的统一释放会忽略它）。
  Conversation conversation(g_stage_count);
  std::vector<std::unique_ptr<Conversation>> multi_conversations;

  Tokenizer* tokenizer = nullptr;
  VocabInfo vocab_info;
  memset(&vocab_info, 0, sizeof(vocab_info));
  embedding_info embed_info;
  struct stat emb_st;
  memset(&emb_st, 0, sizeof(emb_st));

  if (init_tokenizer_and_embedding(tokenizer_path, embedding_path, &vocab_info, &tokenizer, &embed_info, &emb_st) != 0) {
    return -1;
  }

  conversation.result.tokenizer = tokenizer;
  reset_last_stage_result(conversation);

  rknn3_llm_param session_param;
  memset(&session_param, 0, sizeof(session_param));
  session_param.logits_name = (char*)"output";
  session_param.max_context_len = max_context_len;
  session_param.sampling_param.temperature = 1.0f;
  session_param.sampling_param.top_k = 1;
  session_param.sampling_param.top_p = 0.9f;
  session_param.sampling_param.repeat_penalty = 1.0f;
  session_param.sampling_param.frequency_penalty = 0.0f;
  session_param.sampling_param.presence_penalty = 0.0f;
  session_param.vocab_info.vocab_size = vocab_info.vocab_size;
  session_param.vocab_info.n_special_eos_id = vocab_info.n_special_eos_id;
  session_param.vocab_info.n_special_bos_id = vocab_info.n_special_bos_id;
  memcpy(session_param.vocab_info.special_eos_id, vocab_info.special_eos_id, sizeof(vocab_info.special_eos_id));
  memcpy(session_param.vocab_info.special_bos_id, vocab_info.special_bos_id, sizeof(vocab_info.special_bos_id));
  session_param.vocab_info.linefeed_id = vocab_info.linefeed_id;
  session_param.vocab_info.ignore_eos_token = g_ignore_eos ? 1 : 0;

  InputCbUserdata input_cb_data;
  memset(&input_cb_data, 0, sizeof(input_cb_data));

  // 自动查找设备，检查设备数是否足够
  rknn3_devices devs;
  memset(&devs, 0, sizeof(devs));
  int ret = rknn3_find_devices(&devs);
  if (ret != RKNN3_SUCCESS) {
    printf("find devices failed: ret=%d\n", ret);
    release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
    return -1;
  }
  printf("found %d devices:\n", devs.n_devices);
  for (int i = 0; i < devs.n_devices; ++i) {
    printf("  [%d] type=%s, id=%s\n", i, devs.devices[i].type, devs.devices[i].id);
  }

  if (ext_device_ids.empty()) {
    // 未指定外部 device_id，自动分配
    if (devs.n_devices < (int)g_stage_count) {
      printf("auto-detect failed: found=%d devices, need=%zu\n", devs.n_devices, g_stage_count);
      release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
      return -1;
    }
    printf("auto-assigning %zu devices\n", g_stage_count);
  } else if ((int)ext_device_ids.size() < (int)g_stage_count) {
    printf("not enough device_id arguments: need=%zu, got=%zu\n", g_stage_count, ext_device_ids.size());
    release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
    return -1;
  } else {
    // 校验外部指定的 device_id 是否在可用设备列表中
    printf("using external device_id list:\n");
    for (const auto& id : ext_device_ids) {
      printf("  %s\n", id.c_str());
    }
  }

    // 如果提供了 rope 路径，加载 rope caches 并注册 input_callback
  if (rope_path && rope_path[0] != '\0') {
    printf("loading rope cache: %s\n", rope_path);
    if (load_safetensors(rope_path, input_cb_data.rope_caches,
                         &input_cb_data.rope_format,
                         &input_cb_data.rope_fd, &input_cb_data.rope_mmap_base,
                         &input_cb_data.rope_mmap_size) != 0) {
      printf("load_safetensors failed\n");
      release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
      return -1;
    }
  }

  // 第一遍：每卡建 context（权重只加载一次，多会话共享）。
  bool ok = true;
  for (size_t i = 0; i < stages.size(); ++i) {
    const char* device_id = ext_device_ids.empty() ? devs.devices[i].id : ext_device_ids[i].c_str();
    ok = init_stage_context(stages[i], i, device_id,
                            model_paths[i].c_str(), weight_paths[i].c_str(), run_core_mask,
                            &input_cb_data);
    if (!ok) {
      break;
    }
  }
  // 第二遍：在已建好的 context 上开会话。这套 context 是复用的——权重与每卡设备
  // 内存只有一份。未指定 --sessions 时只建 conversation 这一个会话（走原有单会话
  // 路径，行为与改造前逐字节一致）；指定了则建 N 个，每个会话在每张卡上各有一个
  // session。
  if (ok) {
    if (!options.has_sessions) {
      for (size_t i = 0; i < stages.size(); ++i) {
        ok = init_conversation(stages, conversation, i, session_param,
                               tokenizer, &embed_info, &input_cb_data);
        if (!ok) {
          break;
        }
      }
    } else {
      const int session_count = options.sessions;
      for (int s = 0; s < session_count && ok; ++s) {
        std::unique_ptr<Conversation> conv(new Conversation(g_stage_count));
        conv->result.tokenizer = tokenizer;
        reset_last_stage_result(*conv);
        for (size_t i = 0; i < stages.size(); ++i) {
          ok = init_conversation(stages, *conv, i, session_param,
                                 tokenizer, &embed_info, &input_cb_data);
          if (!ok) {
            break;
          }
        }
        // 失败的那个也收进容器，交给下面统一销毁（它可能已经建好了前几段的 session）。
        multi_conversations.push_back(std::move(conv));
      }
      if (ok) {
        printf("[sessions] %d conversations ready on %zu cards\n",
               session_count, stages.size());
      }
    }
  }
  if (!ok) {
    for (auto& conv : multi_conversations) {
      destroy_conversation(*conv);
    }
    multi_conversations.clear();
    release_resources(stages, options.has_sessions ? nullptr : &conversation,
                      &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
    return -1;
  }

  // 资源观测的初值：此刻所有会话都已建好、工作线程还没起，所以这次设备查询是单线程的
  // （refresh_stage_mem_stat 要求调用者持有卡锁，或者处于这个阶段）。
  // 先采一次的价值不只是"页面早点有数"：这个快照就是"这份导出此刻占了多少内存"的
  // 答案，而它决定了还能再开几路——启动横幅下面那几行的账本核对的就是它。
  for (auto& stage : stages) {
    refresh_stage_mem_stat(stage);
  }

  // 检查命令行请求的上下文长度是否超过模型内建的 kvcache_buffer_len。
  if (stages[0].max_ctx_len > 0 && max_context_len > stages[0].max_ctx_len) {
    printf("\n[warning] --ctx-size %d exceeds the model's built-in kvcache_buffer_len %d; "
           "the runtime will fall back to %d.\n"
           "          To use a longer context, re-export the model with a larger "
           "kvcache_buffer_len / max_position_embeddings.\n",
           max_context_len, stages[0].max_ctx_len, stages[0].max_ctx_len);
  }

  std::vector<int32_t> performance_input_tokens;
  if (g_performance_mode) {
    uint64_t required_context_tokens = performance_input_length + performance_output_length - 1;
    for (const auto& stage : stages) {
      if (stage.max_ctx_len > 0 && required_context_tokens > (uint64_t)stage.max_ctx_len) {
        printf("performance lengths exceed %s max context: required=%llu, max=%d\n",
               stage.name.c_str(),
               (unsigned long long)required_context_tokens,
               stage.max_ctx_len);
        release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
        return -1;
      }
    }

    build_performance_input_tokens(vocab_info, performance_input_length,
                                   &performance_input_tokens);
    printf("\n=== Performance Test Mode ===\n");
    printf("Input tokens: %llu, output tokens: %llu\n",
           (unsigned long long)performance_input_length,
           (unsigned long long)performance_output_length);
  }

  // 选择多轮对话的 prompt 模板：默认按外部 rope cache 的 tensor 命名格式判定模型族
  // （Gemma-4 为编号 rope cache，Qwen3.5 为 rope_cos/sin_cache），也可用 --chat-template 覆盖。
  ChatTemplateFamily chat_family =
      (input_cb_data.rope_format == RopeCacheFormat::GEMMA4) ? ChatTemplateFamily::GEMMA4
                                                             : ChatTemplateFamily::QWEN35;
  if (options.chat_template != nullptr && strcmp(options.chat_template, "auto") != 0) {
    chat_family = (strcmp(options.chat_template, "gemma") == 0) ? ChatTemplateFamily::GEMMA4
                                                                : ChatTemplateFamily::QWEN35;
  }
  const ChatTemplateSpec* chat_tpl = chat_template_for(chat_family);
  printf("[chat] multi-turn prompt template: %s\n", chat_template_name(chat_family));
  if (input_cb_data.rope_format == RopeCacheFormat::NONE && options.chat_template == nullptr) {
    printf("[chat] no external rope cache to detect the model family from; "
           "defaulting to qwen3.5 (use --chat-template to override)\n");
  }

  // 未指定 --prompt 时，用所选模板拼一个演示用 prompt。
  std::string default_prompt_buf;
  if (!prompt) {
    default_prompt_buf = chat_tpl->system_prompt;
    default_prompt_buf += chat_tpl->user_prefix;
    default_prompt_buf += "hello";
    default_prompt_buf += chat_tpl->user_postfix;
    prompt = default_prompt_buf.c_str();
  }

  // 多 session 可行性探针：只做测量与隔离性验证，跑完立即退出，不进入对话/性能模式。
  if (options.probe_sessions > 0) {
    int probe_ret = probe_sessions(stages, conversation, session_param, chat_tpl,
                                   options.probe_sessions, tokenizer);
    release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
    return probe_ret == 0 ? 0 : 1;
  }

  // 多会话并发执行（--sessions N）。走到这里说明 N 个 Conversation 已经在每张卡上
  // 各拿到了自己的 session。
  if (options.has_sessions) {
    const int session_count = (int)multi_conversations.size();

    // 逐 token 的 stdout 打印在 N 路并发下必然交错，且 printf 在热路径上会污染
    // 吞吐测量——并发模式一律静音，token 走每个会话自己的 dump 文件。
    // 交互模式例外：它不静音，改用「整段成块」接管输出（见 TokenSink），
    // 静音掉的话就什么都看不见了。服务模式保持静音——它的输出走帧通道，
    // 与这个开关无关（result_callback 里 sink 分支在静音判断之前）。
    g_suppress_generation_output = !options.interactive;

    // 每个会话一个 token dump 文件：N=1 时就是 --dump-tokens 给的原路径（保持与
    // 单会话路径同一份文件、同样内容），N>1 时是 <path>.s<i>。
    std::vector<std::string> dump_paths;
    bool dump_ok = true;
    if (options.dump_tokens && options.dump_tokens[0] != '\0') {
      for (int s = 0; s < session_count; ++s) {
        std::string path = options.dump_tokens;
        if (session_count > 1) {
          path += ".s" + std::to_string(s);
        }
        FILE* fp = fopen(path.c_str(), "w");
        if (!fp) {
          printf("failed to open --dump-tokens file: %s\n", path.c_str());
          dump_ok = false;
          break;
        }
        multi_conversations[s]->result.token_dump = fp;
        dump_paths.push_back(path);
      }
    }
    if (dump_ok) {
      printf("\n=== Multi-Session Mode ===\n");
      const uint64_t context_limit = stages[0].max_ctx_len > 0
                                         ? (uint64_t)stages[0].max_ctx_len
                                         : (uint64_t)max_context_len;
      std::vector<SessionTiming> timings(session_count);
      std::vector<std::thread>   drivers;
      drivers.reserve(session_count);

      if (options.serve) {
        printf("concurrent conversations: %d, serve mode (framed protocol on fd 3)\n",
               session_count);
      } else if (options.interactive) {
        printf("concurrent conversations: %d, interactive (stdin)\n", session_count);
      } else {
        printf("concurrent conversations: %d, rounds per conversation: %d\n",
               session_count, options.rounds);
      }
      for (size_t i = 0; i < dump_paths.size(); ++i) {
        printf("[dump] session %zu token ids -> %s\n", i, dump_paths[i].c_str());
      }

      int rc = 0;
      if (options.interactive || options.serve) {
        // 一条 stdin 输入流 + N 个会话线程共用一条待办队列（谁空闲谁接；
        // 服务模式下还会按 target_session 钉住指定会话）。
        InteractiveDispatcher dispatcher;
        dispatcher.active_workers = session_count;
        // 启动时就全部标活：会话线程还没起，但"现在钉给谁的活都还有希望被接"是真的。
        dispatcher.worker_alive.assign((size_t)session_count, true);

        if (options.serve) {
          // 帧通道要先开好、READY 要先发，再起会话线程：网关靠 READY 判断
          // "后端已就绪"，它必须排在第一个 DELTA 之前，否则启动期就会把帧读串。
          g_serve_out = serve_open_channel(options.serve_fd);
          // READY 带上**真实生效的上下文上限**（模型内建 max_ctx_len，拿不到才退回
          // --ctx-size）。网关的窗口滚动要用它当预算：argv 里的 --ctx-size 会被 runtime
          // 就近夹住（32768 导出请求 8192 仍拿到 32768），照 argv 算会算错。
          serve_frame("READY ",
                      std::to_string(session_count) + " " + std::to_string(max_new_tokens) +
                          " " + std::to_string(context_limit),
                      nullptr, 0);
          printf("protocol frames on fd %d; request: "
                 "REQ <rid> <session| -1> <max_new_tokens> <reset> <prompt_len>\n",
                 options.serve_fd);
        } else {
          printf("Type a message and press Enter; each line goes to an idle session.\n");
          printf("Output is printed as whole blocks prefixed with [s<i>]. Ctrl-D to exit.\n");
        }
        fflush(stdout);

        for (int s = 0; s < session_count; ++s) {
          drivers.emplace_back(run_interactive_session_worker, s,
                               std::ref(*multi_conversations[s]), std::ref(stages),
                               std::cref(vocab_info), chat_tpl, max_new_tokens,
                               context_limit, &dispatcher, &timings[s]);
        }
        // 输入线程与上面 N 个会话线程并发跑；stdin 结束时它会唤醒所有会话线程，
        // 待办队列里剩下的输入仍会被处理完，全部退出后 join 才返回。
        std::thread input_thread = options.serve
            ? std::thread(run_serve_input_worker, &dispatcher, session_count,
                          std::cref(stages), tokenizer)
            : std::thread(run_interactive_input_worker, &dispatcher);
        for (auto& driver : drivers) {
          driver.join();
        }
        input_thread.join();

        report_interactive_sessions(multi_conversations, timings, max_new_tokens);
        rc = 0;
        for (const auto& conv : multi_conversations) {
          if (conv->failed) {
            rc = 1;
            break;
          }
        }
      } else {
        // 同时起跑闸门：不设的话先起的会话会白跑一段，量出来的吞吐虚高。
        StartGate gate(session_count);
        for (int s = 0; s < session_count; ++s) {
          drivers.emplace_back(run_conversation_worker,
                               std::ref(*multi_conversations[s]), std::ref(stages),
                               std::cref(vocab_info), chat_tpl, prompt, max_new_tokens,
                               options.rounds, context_limit, &gate, &timings[s]);
        }
        for (auto& driver : drivers) {
          driver.join();
        }

        rc = report_concurrent_sessions(multi_conversations, timings, prompt,
                                        options.rounds, max_new_tokens);
      }
      print_stage_performance_statistics(stages);
      // 收尾清理：驱动线程在上一行 join 完了，这里没有并发（helper 里的锁是白取的，
      // 但走 helper 是为了不留第二份清 KV 的实现）。
      for (auto& conv : multi_conversations) {
        clear_conversation_kv(stages, *conv);
      }
      for (auto& conv : multi_conversations) {
        if (conv->result.token_dump) {
          fclose(conv->result.token_dump);
          conv->result.token_dump = nullptr;
        }
      }
      for (auto& conv : multi_conversations) {
        destroy_conversation(*conv);
      }
      multi_conversations.clear();
      g_suppress_generation_output = false;
      release_resources(stages, nullptr, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
      return rc;
    }

    // dump 文件打不开：把已经开出来的关掉，再整体释放。
    for (auto& conv : multi_conversations) {
      if (conv->result.token_dump) {
        fclose(conv->result.token_dump);
        conv->result.token_dump = nullptr;
      }
      destroy_conversation(*conv);
    }
    multi_conversations.clear();
    g_suppress_generation_output = false;
    release_resources(stages, nullptr, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
    return -1;
  }

  if (g_interactive) {
    // 多轮交互：keep_history=1 已让 runtime 累积历史 KV cache，
    // 每轮只需把新用户输入按所选模板拼成 prompt 作为 prefill 传入。
    const ChatTemplateSpec* tpl = chat_tpl;

    printf("\n=== Interactive Chat Mode ===\n");
    printf("Type your message and press Enter (Ctrl-D to exit).\n");

    std::string user_input;
    while (true) {
      printf("\nUser: ");
      fflush(stdout);
      if (!read_line_utf8(user_input)) {
        printf("\n");
        break;
      }
      if (user_input.empty()) {
        continue;
      }

      // 仅为「下一轮 prefill」（用户输入 + chat 模板）预留固定余量。
      // 注意：
      //   1) 实际生效上限是模型内建 kvcache_buffer_len（stages[0].max_ctx_len），
      //      而非命令行 --ctx-size（超出会被 runtime 就近回退）。
      //   2) 不能用 max_new_tokens 做余量——decode 通常遇 EOS 就停，实际远用不满；
      //      否则 --predict 较大时会误判“上下文已满”。真正的 decode 溢出由下面
      //      的 context_tokens 累计 + 下一轮 guard 兜底。
      const uint64_t context_limit = stages[0].max_ctx_len > 0
                                         ? (uint64_t)stages[0].max_ctx_len
                                         : (uint64_t)max_context_len;
      const uint64_t prefill_reserve = 512;
      // 上下文累计与"是否首轮"都挂在会话上（P2 起每个会话各有一套）。
      if (conversation.context_tokens > 0 &&
          conversation.context_tokens + prefill_reserve >= context_limit) {
        printf("\n[warning] context %llu/%llu tokens nearly full, clearing KV cache to start a fresh conversation\n",
               (unsigned long long)conversation.context_tokens, (unsigned long long)context_limit);
        clear_conversation_kv(stages, conversation);
      }

      std::string chat_prompt;
      if (conversation.first_turn) {
        // Gemma-4 没有 system role，其 system_prompt 为空串，这里自然退化为不带 system 轮。
        chat_prompt = tpl->system_prompt;
        chat_prompt += tpl->user_prefix;
        chat_prompt += user_input;
        chat_prompt += tpl->user_postfix;
        conversation.first_turn = false;
      } else {
        chat_prompt = tpl->user_prefix;
        chat_prompt += user_input;
        chat_prompt += tpl->user_postfix;
      }

      printf("Assistant: ");
      fflush(stdout);
      ChatTurnResult turn;
      if (!run_chat_turn(stages, conversation, vocab_info, chat_prompt.c_str(),
                         max_new_tokens, &turn)) {
        printf("\n[interactive] inference failed, stopping\n");
        break;
      }
      printf("\n");

      conversation.total_prefill_tokens += turn.prefill_tokens;
      conversation.total_decode_tokens += turn.decode_tokens;
      conversation.total_prefill_ms += turn.prefill_ms;
      conversation.total_decode_ms += turn.decode_ms;
      conversation.context_tokens += turn.prefill_tokens + turn.decode_tokens;
    }

    print_performance_statistics(conversation.total_prefill_tokens,
                                 (float)conversation.total_prefill_ms,
                                 conversation.total_decode_tokens,
                                 (float)conversation.total_decode_ms);
  } else {
    printf("\n=== Prefill Pipeline ===\n");
    timeval prefill_start;
    timeval prefill_end;
    gettimeofday(&prefill_start, NULL);
    uint64_t prefill_tokens = 0;
    const char* prefill_prompt = g_performance_mode ? nullptr : prompt;
    const std::vector<int32_t>* prefill_input_tokens =
        g_performance_mode ? &performance_input_tokens : nullptr;
    if (!run_pipeline_once(stages, conversation, prefill_prompt, prefill_input_tokens,
                           InferencePhase::PREFILL, &prefill_tokens)) {
      printf("prefill failed\n");
      release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
      return -1;
    }
    gettimeofday(&prefill_end, NULL);

    int next_token = -1;
    get_last_stage_token(conversation, &next_token);

    printf("\n=== Decode Loop ===\n");
    if (next_token < 0) {
      printf("prefill did not return token from result_callback\n");
      release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);
      return -1;
    }
    timeval decode_start;
    timeval decode_end;
    gettimeofday(&decode_start, NULL);
    uint64_t decode_tokens = 0;
    uint64_t decode_steps = g_performance_mode
                                ? performance_output_length - 1
                                : (max_new_tokens > 0 ? (uint64_t)max_new_tokens : 0);
    for (uint64_t step = 0; step < decode_steps && next_token >= 0; ++step) {
      std::vector<int32_t> token_vec(1, next_token);
      VLOG("[Decode %llu] token=%d\n", (unsigned long long)(step + 1), next_token);

      if (!run_pipeline_once(stages, conversation, nullptr, &token_vec,
                             InferencePhase::DECODE, nullptr)) {
        printf("decode step %llu failed\n", (unsigned long long)(step + 1));
        break;
      }

      if (!get_last_stage_token(conversation, &next_token)) {
        printf("decode step %llu did not return token from result_callback\n",
               (unsigned long long)(step + 1));
        break;
      }
      decode_tokens += 1;
      if (!g_performance_mode && !g_ignore_eos &&
          next_token == vocab_info.special_eos_id[0]) {
        VLOG("\ndecode step %llu reached EOS token\n", (unsigned long long)(step + 1));
        break;
      }
    }
    gettimeofday(&decode_end, NULL);

    float prefill_ms = elapsed_us(prefill_start, prefill_end) / 1e3f;
    float decode_ms  = elapsed_us(decode_start, decode_end) / 1e3f;
    if (g_performance_mode) {
      printf("\nPerformance Test Lengths: input=%llu/%llu, output=%llu/%llu\n",
             (unsigned long long)prefill_tokens,
             (unsigned long long)performance_input_length,
             (unsigned long long)(decode_tokens + 1),
             (unsigned long long)performance_output_length);
    }
    print_performance_statistics(prefill_tokens, prefill_ms, decode_tokens, decode_ms);
  }

  if (g_token_dump) {
    fclose(g_token_dump);
    g_token_dump = nullptr;
  }

  print_stage_performance_statistics(stages);
  // 收尾清理（单会话路径）：跑到这里已经没有任何在途轮次，锁是白取的，走 helper
  // 只为不留第二份实现。
  clear_conversation_kv(stages, conversation);

  release_resources(stages, &conversation, &input_cb_data, &embed_info, emb_st.st_size, tokenizer);

  return 0;
}
