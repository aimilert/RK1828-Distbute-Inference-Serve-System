// demo_4chat.html 的自动化检查：用**桩 DOM**把页面里那段真实 script 跑起来，打到真板卡上。
//
// 为什么不另写一份等价代码来测：那样测的是"我以为页面在做什么"，不是页面本身。
// 这里跑的就是 `<script>` 里的原文（正则抽出来原样喂给 vm），只有 DOM 是假的。
//
// 它守的几条都是**踩过坑才加的**：
//   1. 每一路必须各自带**显式身份**（`X-Conversation-Id: web-<n>`）—— 靠"首问不同"认对话
//      是会撞的，而且撞了之后答案全对、只是变慢，屏幕上根本看不出来
//   2. 每一路占到的会话，在 `/v1/pool` 里必须记着**它自己的**名字 —— 光看会话号还不够：
//      身份没送出去的话会话号照样是四个不同值，但占的是四段**匿名**对话
//   3. 关页面 / 点「清空」都必须把会话**真的还回去**（两条不同的代码：sendBeacon 与 fetch）
//      —— 页面占着 4 个会话不放，会把后来的客户端（终端、第二个浏览器）全堵在队列里，
//      直到 IDLE_TTL 超时
//   4. 四个默认问题不许有重复     —— 现在并发不靠它了，这是演示素材的抄写检查
//   5. 流结束后必须清掉 active    —— render() 曾经把刚算好的 tok/s 统计行覆盖掉
//   6. 四路必须落在四个不同会话   —— 退化成串行时，屏幕上看不出来，只有会话号能戳穿
//   7. 统计行必须带会话号         —— 拿不到 X-KV-Reuse 时（缺 Expose-Headers）会静默变空
//   8. 页面要的 id 必须真实存在   —— 桩不再给不存在的 id 现造元素，拼错的 id 会像浏览器一样
//     返回 null（见 byId 那段注释）
//   9. 面板头的性能小字必须写出来 —— `data-role="perf"` 拼错、元素被删、setPerf 忘了调，
//     这三种坏法页面都照跑照出答案，只是那格永远空着（2026-09-18 加）
//  10. 新一轮开始时那格必须是空的 —— 挂着**上一轮**的首字延迟/速率，在最该判断"到底开始
//     没有"的那一帧上给的是旧数字，比空着更坏（2026-09-18 加）
//  11. 汇总那一句的两种"平均"不许混 —— 各路速率和 Σ(tok_i/el_i) 与单路基线**同口径**，
//     伸缩比只能用它；墙钟摊薄 Σtok/墙钟 在四路长短不齐时会被空转的尾巴摊小。用户
//     2026-09-18 报过这次真事：一路跑 135.7s、三路约 16s，屏幕上给"聚合 15.43 tok/s"，
//     看着跟单路基线 10.6 是一路的。下面那组 fixture 拿那几个真实数钉死两个数都要算对
//  12. 被摊薄/排过队必须当场说破 —— 稀释时不报"平均只有 1.37/4 路在跑"、排队时不标
//     "伸缩比不可引用"，这两种数就会被当成并发性能读走。**数没错，是没人知道它是什么**
//  13. 窗口数上限必须是**问后端要的**，且「＋」到上限真的加不动（2026-09-20 加）——
//     页面自己写死 4 的话，NSESSION=2 时它照样让你开第 3、4 个窗口（那两格只会排队，
//     屏幕上看着像"并发退化了"）；最坏的一种是拿 32K 导出去凑 4 路，那是把板卡压死、
//     要重启才能恢复的那条路。上限错了不会报错，只会让演示里的数变成假的
//  14. 增删窗口不许复用身份、删窗口要**当场交还会话**（2026-09-20 加）—— 用"第几格"当
//     身份，删中间一格再补一个，新格子正好捡回旧名字，网关认成同一段旧对话（拿旧历史、
//     命中旧 KV），现场看着就是"删了没删掉"；不交还会话则要等到 IDLE_TTL 才有位置，
//     而这期间屏幕上什么异常都看不出来
//  15. 高度预算：整页正好一屏、对话区吃掉剩余高度、资源块封顶 1/4 屏（2026-09-20 加）——
//     用户报"对话框下面空一大片"。根因是面板写死 `calc(50vh - 60px)`、资源区按内容长，
//     三块加起来不到一屏。这类坏法**别的检查一条都照不到**：答案照样对、资源条照样刷、
//     会话照样还，只有把页面真的渲染出来才看得见。桩 DOM 没有排版引擎，所以这一节
//     解析 `<style>` 源码判高度关系（注释先剥掉，见那一段）。
//  16. 底部七块卡片**一样高、两条底边齐**（2026-09-20 加）—— 用户报"高低不平"。等高是
//     三步叠出来的（外层拉伸 / 子网格 align-content 拉伸 / 卡片被拉满），**缺任何一步都
//     不齐，而每一步单独看都像是对的**；列数还得写死，auto-fit 会在 1500px 以下自己折行
//     （1366 折成 3+1、1280 折成 2+1），一折行高度立刻对不上、整块还更高。「服务」那五行
//     改两列同样只靠一个类名联系着 JS 与 CSS。这些坏法**功能性检查一条都照不到**：
//     答案照样对、会话照样还、资源条照样刷。
//  17. 宽度不够时该"整段折"、不该"断词"、更不该溢出（2026-09-20 加）—— 温度那一行在
//     1600/1366/1280 上真的**溢出过卡片边框**（`.m` 是 nowrap，文字压在隔壁那张卡上），
//     而"内存"那一行会在自己内部断成四截。改法是让行可折、让尾注成为一个整体。
//  18. 温度要**看得见**（2026-09-20 加，用户报"3588 的温度没有用合适的方法展示出来"、
//     "1828 的温度没有展示出来"）—— 三件独立的事：① RK3588 的**七个热区全画出来**
//     （原来只画最热的 3 个 + 一个 "+4"，那四个在屏幕上没有任何入口）、**顺序固定**
//     （按温度排的话每 2 秒重画一次、整行会来回重排）、缩写要能分辨（bigcore0/bigcore1
//     截断成两个 "bigcor…" 分不清）；② 四张卡的结温**每张一行**，带那一份文件有多旧
//     （它是最多 5 秒前另一个进程采的，不是这一秒读的）；③ 缺值一律 "—"、**绝不当 0°**
//     ——0° 是一句肯定的假话（"这张卡凉快"）。①②③ 都是"屏幕上有东西但不对/没有"，
//     功能性检查一条都照不到：接口照样 200、数字照样是数。
//
// 用法（任意目录；演示页按 __dirname/../gateway/demo_4chat.html 定位）：
//   node page_check.js http://<板卡IP>:18280
//   MAXTOK=64 node page_check.js http://<板卡IP>:18280    # 只改本次演示的每轮长度，不动页面默认值
//
// 需要 node。后端**不一定是板卡**：对着本地桩后端（`fake_backend.py`，起法见 README.md 测试
// 一节）就能整跑，2026-09-18 实测全绿——改完这一页先在本地过一遍，省一次板卡往返。桩验不
// 到的是真后端的**长度与内容**行为（截断、吐不吐 <think>），那些还得上板。
// 板卡是台**在用的**机器：对着真板卡跑时，别人开着的 /demo 页面会占着 `id:web-*` 的会话，
// 开工时先拍一张池子快照，只判这一轮自己开的那些（2026-09-20 加，起因见那段注释）。
// 不进 run_all_board_tests.sh（那套是纯 python3 的）。
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BASE = process.argv[2] || "http://127.0.0.1:18280";
const PAGE = path.join(__dirname, "..", "gateway", "demo_4chat.html");

if (!fs.existsSync(PAGE)) {
  console.log("FAIL: 找不到演示页 " + PAGE);
  process.exit(1);
}

// ---------- 极简 DOM 桩 ----------
function makeEl(tag) {
  const el = {
    tagName: tag, className: "", value: "", checked: false, disabled: false,
    style: {}, children: [], _text: "", _html: "", _byRole: {}, _byClass: {},
    _listeners: {},
    classList: {
      _s: new Set(),
      toggle(c, on) { if (on === undefined) { on = !this._s.has(c); } on ? this._s.add(c) : this._s.delete(c); },
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    // 页面删窗口时用 el.remove()（浏览器里是标准方法，桩以前没有）。漏了它的后果
    // 不是"删不掉"，而是那条路在桩里直接 TypeError——于是"删窗口之后身份有没有被
    // 复用"这件最该检查的事，恰恰是桩里唯一跑不到的地方。
    remove() {
      if (!this.parentNode) { return; }
      const arr = this.parentNode.children || [];
      const i = arr.indexOf(this);
      if (i >= 0) { arr.splice(i, 1); }
      this.parentNode = null;
    },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    querySelector(sel) {
      if (sel[0] === "[") {
        const m = /\[data-role=["']?([\w-]+)["']?\]/.exec(sel);
        return m ? (this._byRole[m[1]] || null) : null;
      }
      if (sel[0] === ".") { return this._byClass[sel.slice(1)] || null; }
      return null;
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      // innerHTML 里的 data-role="x" 和 class="dot" 建成子元素，供 querySelector 找到
      for (const m of v.matchAll(/data-role=["']?([\w-]+)["']?/g)) {
        if (!this._byRole[m[1]]) { this._byRole[m[1]] = makeEl("input"); }
      }
      for (const m of v.matchAll(/class=["']([\w\s-]+)["']/g)) {
        for (const c of m[1].split(/\s+/)) {
          if (c && !this._byClass[c]) { this._byClass[c] = makeEl("span"); }
        }
      }
    },
  };
  return el;
}

// ---------- 抽页面里的 script 原样跑 ----------
const html = fs.readFileSync(PAGE, "utf8");

// 页面里**静态声明**的 id 全部先建出来（下面 getElementById 只认这些）。
// 为什么不是"要什么就现造一个"（2026-09-16 修）：桩以前是
// `getElementById(id) { return (byId[id] = byId[id] || makeEl("div")); }` —— 任何 id
// 都能凭空拿到一个元素。于是页面把 `getElementById("gstat")` 拼错成少一个 s，桩给一个
// 空 div，检查照跑照绿；真浏览器里拿到的是 null，下一行取属性就 TypeError，整页白屏。
// 也就是说**页面上的 id 拼写错误是这套检查唯一测不出来的东西**，而它正是最容易犯的错
// （改页面时顺手改了 id、忘了改 JS）。现在只认 HTML 里真实存在的 id，要别的就返回 null
// ——和浏览器一样，拼错立刻暴露。
const byId = {};
for (const m of html.matchAll(/\bid="([\w-]+)"/g)) {
  if (!byId[m[1]]) { byId[m[1]] = makeEl("div"); }
}
const missingIds = [];
const document = {
  getElementById(id) {
    if (byId[id]) { return byId[id]; }
    missingIds.push(id);
    console.log("  FAIL: 页面 getElementById(" + JSON.stringify(id)
                + ") —— HTML 里没有这个 id（拼错了？浏览器这里会返回 null 并抛 TypeError）");
    return null;
  },
  createElement(tag) { return makeEl(tag); },
  // 资源条（2026-09-20 加）在页面切走时停轮询、切回来立刻补一次。桩得给这两样：
  // 少了 addEventListener，页面启动那行直接 TypeError，整页在桩里全灭——而"切走就
  // 停轮询"这件事恰恰是**只有桩能验**的（浏览器里要人手动切标签页）。
  hidden: false,
  _listeners: {},
  addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
};

let script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];

// MAXTOK=64 node page_check.js … —— 只改**本次演示**的每轮长度，不动页面默认值。
//
// 页面把输出上限做成输入框之后（原来写死成源码里的 `const MAXTOK = 256;`），这里不再去
// 改页面源码里的常量，而是**像用户一样把值交进去**：页面启动时先读 localStorage 里记住的
// 上次设定（demo_4chat.html 的 initMaxtok），桩只要往那个 store 里放一个值，页面就会自己
// 把它填进输入框、并带着它发请求——和真浏览器走的是同一条路径，也就不存在"改源码改出来的
// 行为跟用户点出来的不一样"这种事。
const LS_KEY = "demo4chat.maxtok";
const lsStore = {};
if (process.env.MAXTOK) {
  lsStore[LS_KEY] = process.env.MAXTOK;
  console.log("（本次 MAXTOK=" + process.env.MAXTOK + "）");
}
// 记下每次请求带的身份头：验证"四路确实是以四段不同对话的身份发出去的"，不能只看结果。
const sentIds = [];
// 也把请求体记下来。"输出上限"从源码常量变成输入框之后，"输入框里的值有没有真的被
// 带出去"就成了一件**看不出来**的事：页面照跑、答案照出，只是长度还是老的默认值。
// 光看结果分不出"模型自己停的"和"上限没送出去"，所以直接查请求体。
const sentBodies = [];
const abs = (url) => (url.startsWith("http") ? url : BASE + url);
const winListeners = {};
const ctx = {
  document, performance, TextDecoder, console, JSON, Promise, Math, Date,
  setTimeout, Error,
  // 页面用 setInterval 在"还没拿到会话"的那段时间里每 200ms 刷新一次统计行（2026-09-18
  // 加：那一刻页面上本来只有一个转圈，看着像卡死）。桩里必须是**真的**定时器，不能是
  // 空函数——空函数会让踩醒间隔的那条路在桩里悄悄走不通，绿了也说明不了页面没问题
  // （这次就是先漏了它，页面在桩里直接报 `setInterval is not defined`，四路全灭）。
  setInterval, clearInterval,
  // 资源条的自排下一拍用的是 setTimeout/clearTimeout（串行，慢响应不会把请求叠起来）。
  // 同上：桩不能摆空函数，否则"切走之后真的不再发请求"这条路径在桩里根本走不到。
  clearTimeout,
  // 页面里的 API 是 ""（同源相对路径），浏览器会自动按页面来源补全；node 不会，
  // 所以这里替它补——**只是替浏览器补全这一步，请求本身没做任何改写**。
  fetch: (url, opts) => {
    // 只记**对话请求**的身份和请求体。页面 2026-09-20 起还会 GET /health 拿窗口数上限
    // （见 demo_4chat.html 的 MAX_SESSIONS），把它也算进来就会往下面那两条逐字比对的
    // 断言里插一个 null——而那两条守的是"每路有没有带着自己的身份发出去""输入框里的
    // 输出上限有没有真的被带上"，跟健康检查没有半点关系。
    if (String(url).indexOf("/v1/chat/completions") >= 0) {
      const h = (opts && opts.headers) || {};
      sentIds.push(h["X-Conversation-Id"] || null);
      try { sentBodies.push(JSON.parse(opts.body)); } catch (e) { sentBodies.push(null); }
    }
    return fetch(abs(url), opts);
  },
  // 页面用 localStorage 记住上次的输出上限。桩给一个**真的小 store**，而不是一对空函数：
  // 空函数会让"设了值 → 刷新还在"这条路径在桩里悄悄走不通，绿了也说明不了页面没问题。
  localStorage: {
    getItem(k) { return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; },
    setItem(k, v) { lsStore[k] = String(v); },
    removeItem(k) { delete lsStore[k]; },
  },
  // 页面用到的浏览器全局。桩的职责是**把环境补齐**，页面代码不该为了能在桩里跑而变形。
  window: {
    addEventListener(ev, fn) { (winListeners[ev] = winListeners[ev] || []).push(fn); },
  },
  // sendBeacon 在桩里**真的把那句话发出去**（浏览器里也是这么发的）。要是这里摆个空函数，
  // "关页面有没有把会话还回去"就只能靠嘴说了——而这恰恰是最容易泄漏的一条路。
  navigator: {
    sendBeacon(url, body) {
      fetch(abs(url), { method: "POST", body: body,
                        headers: { "Content-Type": "text/plain;charset=UTF-8" } })
        .catch(() => {});
      return true;
    },
  },
};
ctx.globalThis = ctx;
// 页面脚本没导出任何东西，补一行把内部函数交出来供测试调用
vm.createContext(ctx);
vm.runInContext(script + "\n;globalThis.__t = { panels: panels, send: send, "
               + "streamChat: streamChat, updateGlobal: updateGlobal, "
               + "summarize: summarize, runAll: runAll, "
               + "lastRound: function () { return lastRound; }, "
               // 基线速率是页面里一个会被改的变量，"交出当时的值"没有意义——
               // 要的是**读得到当下的值**，所以交一个闭包而不是快照。
               + "baselineRate: function () { return lastSingleRate; }, "
               // 窗口数上限这一整套（2026-09-20 加）。上限是从后端 /health 的 ctx_size
               // 反推出来的，页面启动时**先查再建窗口**，所以 `capReady` 是个 Promise：
               // 检查必须 await 它，否则量到的是"还没建窗口"的中间态，而那种空跑照样
               // 全绿。`maxSessions`/`ctxSize` 同理要交闭包——它们是 let，快照会过期。
               + "capReady: capReady, addPanel: addPanel, removePanel: removePanel, "
               + "maxSessions: function () { return MAX_SESSIONS; }, "
               + "ctxSize: function () { return CTX_SIZE; }, "
               + "capHtml: function () { return document.getElementById(\"cap\").innerHTML; }, "
               + "capOver: function () { return document.getElementById(\"cap\").className; }, "
               // 资源条这一整套（2026-09-20 加）。**把 renderSys 本身交出来**，检查才
               // 能拿一份自己造的 /v1/system 去渲染——本机没有 /proc，真跑起来 RK3588
               // 那半永远都是 "—"，那些数字格式（GB/MB、每核、°C、旧数据的标注）就
               // 只能等上了板子靠眼睛看。这与 summarize 那一组 fixture 是同一个套路：
               // 页面跑的是原函数，喂进去的是一份确定的输入。
               + "renderSys: renderSys, renderSysErr: renderSysErr, "
               + "sysHtml: function () { return document.getElementById(\"sys\").innerHTML; }, "
               + "startSysPoll: startSysPoll, stopSysPoll: stopSysPoll, "
               + "refreshSystem: refreshSystem, "
               + "sysOn: function () { return sysOn; }, "
               // "这一块有多贵、省在哪"这几条要能被验到：刷新档位（生成中/待机）、
               // 画过几次、以及"同一份 doc 来了要不要重画"那道闸。都是 let，交闭包。
               + "sysIntervalMs: sysIntervalMs, sysWake: sysWake, "
               + "sysRenders: function () { return sysRenders; }, "
               + "sysLastTs: function () { return sysLastTs; }, "
               + "SYS_POLL_BUSY_MS: SYS_POLL_BUSY_MS, "
               + "SYS_POLL_IDLE_MS: SYS_POLL_IDLE_MS, "
               + "SYS_STALE_S: SYS_STALE_S, "
               // 卡温那一份的陈旧阈值（2026-09-20）。**必须和 SYS_STALE_S 分开交出来**：
               // 检查里那条"两个阈值不许是同一个数"要靠它们比。
               + "TEMP_STALE_S: TEMP_STALE_S, "
               // 暂停/继续（2026-09-20）：render 只负责把 streaming/paused 拨到
               // 按钮上，检查直接拿一个真面板拨布尔、看按钮变没变。
               + "render: render, pauseTurn: pauseTurn, continueAnswer: continueAnswer "
               + "};", ctx);

const { panels, send, summarize, addPanel, removePanel, capReady } = ctx.__t;

// ---------- 汇总那一句的算术（纯函数，不联网） ----------
//
// 2026-09-18 用户报的："合计 2095 tok / 墙钟 135.7s → 聚合 15.43 tok/s"，看着跟单路基线
// 10.6 是一路的，于是"四路并发没什么用"就是那么读出来的。**问题不在数，在口径**：
// 那 135.7s 里只有前 18 秒是四路在跑，后面 120 秒是**一路**在长生成（1602 tok），
// 把"一路的吞吐"摊进了"四路并发"的分母。所以这里钉死两个数**都算出来、且不混淆**，
// 并且钉死**伸缩比只许用各路速率和**（口径与基线一致）。
//
// 这几个数是从用户那次真实运行抄下来的，不是编的：改了公式而忘了改断言，这一节会报 FAIL。
const near = (got, want, tol) => Math.abs(got - want) <= (tol === undefined ? 0.011 : tol);
const grab = (html, re) => { const m = html.match(re); return m ? parseFloat(m[1]) : NaN; };

function fixture(name, rs, wall, baseline) {
  const html = summarize(rs, wall, baseline);
  console.log("  [%s] %s", name, html.replace(/<[^>]*>/g, ""));
  return html;
}

{
  let bad = 0;
  const chk = (ok, msg) => { if (!ok) { console.log("  FAIL: " + msg); bad++; } };

  // ① 用户那次真事的数字：四路 153/182/1602/158 tok，墙钟 135.7s
  const long = [{ tok: 153, el: 15.6, waited: 0 }, { tok: 182, el: 18.1, waited: 0 },
                { tok: 1602, el: 135.7, waited: 0 }, { tok: 158, el: 16.3, waited: 0 }];
  const h1 = fixture("长短不齐", long, 135.7, 10.63);
  chk(near(grab(h1, /速率和 <b>([\d.]+)/), 41.36), "各路速率和应为 41.36 tok/s（实测值）");
  chk(near(grab(h1, /墙钟摊薄 ([\d.]+)/), 15.44), "墙钟摊薄应为 15.44 tok/s（实测值）");
  chk(/1\.37\/4 路在跑/.test(h1), "应报出「平均只有 1.37/4 路在跑」（稀释诊断）");
  chk(near(grab(h1, /伸缩 ([\d.]+)×/), 3.89), "伸缩比应为 3.89× = 41.36/10.63（用速率和，不是摊薄值）");
  // 摊薄值除以基线只有 1.45×，看着像"四路并发毫无用处"——那正是用户报的读数，
  // 所以这条不许只查"等于 3.89"，还要查它**大于**摊薄口径算出来的值。
  chk(grab(h1, /伸缩 ([\d.]+)×/) > 2,
      "伸缩比不许用摊薄值算（否则得到 1.45×，屏幕上就是「并发没用」）");
  // 这两条是"口径不许混"的正面断言：主数必须是速率和，摊薄值必须戴着它的帽子出现
  chk(h1.indexOf("各路速率和") < h1.indexOf("墙钟摊薄"), "速率和必须排在摊薄值前面（它是主数）");
  chk(/别当并发性能看/.test(h1), "摊薄时有义务说明「这不是并发性能」");
  chk(/合计 2095 tok/.test(h1), "合计 token 数应为 2095");

  // ② 四路等长：两个数本来就该接近，这时摊薄值是可以看的，且不许报稀释
  const even = [{ tok: 150, el: 15, waited: 0 }, { tok: 150, el: 15, waited: 0 },
                { tok: 150, el: 15, waited: 0 }, { tok: 150, el: 15, waited: 0 }];
  const h2 = fixture("四路等长", even, 15.1, null);
  chk(near(grab(h2, /速率和 <b>([\d.]+)/), 40.00, 0.005), "速率和应为 40.00 tok/s");
  chk(near(grab(h2, /墙钟摊薄 ([\d.]+)/), 39.74), "摊薄应为 39.74 tok/s");
  chk(!/长短不齐/.test(h2), "四路等长时不该报稀释警告（假警报会让人不再信这条）");
  chk(/二者等价/.test(h2), "等长时应说明两个数等价");
  chk(/先点「①」/.test(h2), "没有基线时应提示去量基线，不许默默不显示伸缩比");

  // ③ 排过队：伸缩比必须当场标成不可引用（排队混在墙钟里，数看着像"并发退化"）
  const q = [{ tok: 153, el: 15.6, waited: 0 }, { tok: 182, el: 18.1, waited: 40.2 },
             { tok: 1602, el: 135.7, waited: 0 }, { tok: 158, el: 16.3, waited: 0 }];
  const h3 = fixture("某路排队", q, 135.7, 10.63);
  chk(/有 1 路排过队（最多 40\.2s），上面的数含伸缩比都不可引用/.test(h3),
      "应报出排队并声明伸缩比不可引用");
  chk(/伸缩 3\.89×/.test(h3), "排队时数照样显示，只是标明不可引用");
  chk(h3.indexOf("伸缩 3.89×") < h3.indexOf("排过队"),
      "排队警告必须排在伸缩比之后（它撤的就是刚印出来的那个数）");
  chk(!/排过队/.test(h1), "没人排队时不许报排队（假警报会把真警报淹掉）");

  // ④ 只有两路（2026-09-20 加）：末句里的路数必须是**当时那几路**，不能写死"四路"。
  // 32K 那份导出只够 2 路会话，页面就只开 2 个窗口——那时屏幕上写"四路长短不齐"是假话，
  // 而这句话正是判读那两个数的关键（它解释的是分母里混进了什么）。
  const two = [{ tok: 300, el: 30, waited: 0 }, { tok: 20, el: 2, waited: 0 }];
  const h4 = fixture("两路", two, 30.0, 10.63);
  chk(/2 路并发/.test(h4), "两路时开头该写「2 路并发」而不是「4 路并发」");
  chk(/2 路长短不齐/.test(h4), "两路时稀释警告该写「2 路长短不齐」（写死成「四路」是假话）");
  chk(!/四路/.test(h4), "两路时整句里不该出现「四路」（把句数写死是演示里最容易出的假话）");
  chk(near(grab(h4, /平均只有 ([\d.]+)\/2 路在跑/), 1.07, 0.02), "平均在跑路数该是 1.07/2");
  const h5 = fixture("两路齐", [{ tok: 100, el: 10, waited: 0 }, { tok: 100, el: 10, waited: 0 }],
                    10.05, null);
  chk(/2 路差不多同时收工/.test(h5), "两路等长时该写「2 路差不多同时收工」");
  chk(!/四路/.test(h5), "两路时整句里不该出现「四路」");

  if (bad) {
    console.log("FAIL: 汇总算术有 %d 条不对（上面每一条都对着真实运行数钉过）", bad);
    process.exit(1);
  }
  console.log("  汇总算术 5 组 fixture 全过\n");
}

// 统计行是**HTML**（速率要加粗，见页面里的 setStat），桩又把 _text 与 _html 分开存
// （和浏览器一样），所以读 `stat.textContent` 会拿到空串。2026-09-18 修：页面把 setStat
// 从 textContent 改成 innerHTML 时，这里读的字段没跟着改，`/tok\/s/` 那条断言就会
// 在页面完全正常的情况下报 FAIL——**空跑也像失败**，比不检查更费时间。
// 这个取值器两边都认，改哪一边都不会再假装报错。
const statOf = (el) => el.textContent || el.innerHTML;
const stripHtml = (s) => String(s).replace(/<[^>]*>/g, "");
const poolSlots = async () => ((await (await fetch(BASE + "/v1/pool")).json()).slots || []);
const webHeld = (slots) => slots.map(s => s.key).filter(x => x && x.startsWith("id:web-"));

(async () => {
  // 等 `id:web-*` 全部从会话池里消失（交还是异步发出的，给最多 4 秒）。
  // 返回**还占着的那几个**：空数组才算干净，非空就直接进 FAIL 信息里。
  async function waitReleased() {
    let held = [];
    for (let k = 0; k < 40; k++) {
      // 只判**这一轮自己开的**那些身份（见下面 foreignWeb）。
      held = webHeld(await poolSlots()).filter(x => !foreignWeb.has(x));
      if (!held.length) { return []; }
      await new Promise(r => setTimeout(r, 100));
    }
    return held;
  }

  // 开工之前先给会话池拍一张快照：**来的时候就占着的 `id:web-*` 是别人的**，不是这一轮开的。
  // 板卡是台在用的机器——有人开着 /demo 页面，池子里本来就躺着 `id:web-1` 这样的租约，而这
  // 份检查只认前缀，于是"删窗口没把会话还回去"会指着一份**跟它无关**的会话报 FAIL，还顺手
  // 让整份检查在这一节 `exit(1)`（后面几节根本不跑）。2026-09-20 板上真事：一个浏览器开着
  // 页面，两轮检查全红在这里，看着像页面坏了。别人占着会说一句，但不影响判定。
  const foreignWeb = new Set(webHeld(await poolSlots()));
  if (foreignWeb.size) {
    console.log("（来的时候池子里就占着 %s —— 这台板卡上还有别的页面/客户端在用，"
                + "下面只判这一轮自己开的那些身份）", JSON.stringify([...foreignWeb]));
  }

  let bad0 = 0;

  // ---------- 建窗：窗口数上限是**问后端要的**，不是页面自己定的 ----------
  // 这一节以前是在顶层直接 `if (panels.length !== 4)`。改成 async 不是风格问题：
  // 页面 2026-09-20 起先 GET /health 拿到后端会话数**再**建窗口，顶层那一刻窗口还是 0 个，
  // 而"0 个"如果也当通过，这一整段就成了空跑——**空跑全绿比报错更难发现**。
  await capReady;
  const health = await (await fetch(BASE + "/health")).json();
  console.log("后端 /health: sessions=%s ctx_size=%s",
              health.sessions, health.ctx_size);
  console.log("页面读到: 会话 %d · 上下文 %s · 窗口 %d 个",
              ctx.__t.maxSessions(), ctx.__t.ctxSize(), panels.length);

  // 上限必须**等于后端真开出来的会话数**。页面要是自己写死 4：换成 NSESSION=2 起后端，
  // 它照样让你开第 3、第 4 个窗口，那两格只会排队（屏幕上看着像"并发退化了"）；
  // 更坏的是拿 32K 导出去凑 4 路——那是把板卡压死要重启的那条路（见 memory
  // qwen35-ctx-export：32K 每卡只够 2 路）。
  if (ctx.__t.maxSessions() !== health.sessions) {
    console.log("FAIL: 页面上限 %d ≠ 后端 /health 的 sessions %d（上限是自己编的，不是问来的）",
                ctx.__t.maxSessions(), health.sessions);
    bad0++;
  }
  if (ctx.__t.ctxSize() !== health.ctx_size) {
    console.log("FAIL: 页面显示的上下文 %s ≠ 后端 /health 的 ctx_size %s",
                ctx.__t.ctxSize(), health.ctx_size);
    bad0++;
  }
  const wantPanels = Math.min(4, health.sessions);
  if (panels.length !== wantPanels) {
    console.log("FAIL: 初始窗口数该是 min(4, 后端会话数) = %d 个，实际 %d 个",
                wantPanels, panels.length);
    bad0++;
  }

  const capText = stripHtml(ctx.__t.capHtml());
  console.log("容量条:", capText);
  // 容量条要**一直**回答"为什么我只能开这几个窗口"，所以这三个数必须都在上面。
  if (!/上下文/.test(capText) || !/后端会话/.test(capText) || !/窗口 \d+\/\d+/.test(capText)) {
    console.log("FAIL: 容量条没把「上下文 / 后端会话 / 窗口 n/N」说全：" + JSON.stringify(capText));
    bad0++;
  }
  // 分子是**屏幕上真摆着的窗口数**、分母是上限，两者不等才是正常的（上限 6 时屏幕只摆 4 个）。
  const capWin = capText.match(/窗口 (\d+)\/(\d+)/);
  if (!capWin || Number(capWin[1]) !== panels.length || Number(capWin[2]) !== health.sessions) {
    console.log("FAIL: 容量条上的窗口数与实际不符（该是 窗口 %d/%d）：%s",
                panels.length, health.sessions, JSON.stringify(capText));
    bad0++;
  }
  // 「②」的文案要跟着窗口数走。写死"4 路"而实际只开 2 个窗口，是在演示里说假话。
  const allText = stripHtml(byId["all"].textContent);
  if (allText.indexOf("② " + panels.length + " 路") < 0) {
    console.log("FAIL: 「②」的文案没跟着窗口数走（窗口 %d 个，按钮写着 %s）",
                panels.length, JSON.stringify(allText));
    bad0++;
  }

  const qs = panels.map(p => p.input.value);
  console.log("默认问题:", JSON.stringify(qs, null, 0));
  if (new Set(qs).size !== qs.length) {
    // 各窗口是不是各段对话，现在由显式身份保证，**不再依赖问题不同**——所以这不是
    // 并发问题，是素材问题：这几问是挑过的演示默认值（见 demo_4chat.html 的注释），
    // 短了会参差、长了会顶到 max_tokens，重复说明改的时候抄错了。
    console.log("FAIL: 默认问题有重复（不影响并发，但这几个默认值是挑过的，请对齐页面注释）");
    bad0++;
  }
  if (bad0) {
    console.log("FAIL: 建窗这一节有 %d 项不对", bad0);
    process.exit(1);
  }
  console.log("  建窗与容量条全过\n");

  // ---------- 增删窗口：这一页新功能的正身 ----------
  // 用户 2026-09-20 要的三件事，逐条钉死。三条都是**照跑照出答案、屏幕上却不对**的坏法：
  //   a) 「＋」加上去的窗口必须有**自己的新身份**——复用旧身份的话，网关按 key 认对话，
  //      新窗口第一个问题就命中旧窗口的 KV、还接着旧历史，现场看着就是"删了没删掉"
  //   b) 加到上限就**真的加不动**（按钮禁用 + 点了不涨）。上限是后端容量，不是屏幕摆得下几个
  //   c) 删窗口必须**当场把会话还回去**。网关只排队不抢占，不还的话新窗口/新客户端要一直
  //      等到 IDLE_TTL（默认 300s）——而这几分钟里屏幕上什么异常都看不出来
  {
    const cap = health.sessions;
    const usedIds = new Set(panels.map(p => p.convId));
    const btn = byId["addwin"];
    const idPane = (p) => (p ? p.convId : null);
    let bad1 = 0;

    // 面板显示号（"面板 3"）在增删之后必须**重排**，而对话身份（convId）不许动。
    // 序号是给眼睛看的，身份是给网关认对话的；两者一旦绑在一起，删掉中间那格就会让
    // 后面几格全都"换了一段对话"——KV 复用直接没了，屏幕上一点异常都看不出来。
    const namesOk = () => {
      const got = panels.map(p => p.name.textContent);
      const want = panels.map((p, i) => "面板 " + (i + 1));
      if (JSON.stringify(got) !== JSON.stringify(want)) {
        console.log("FAIL: 面板显示号没重排：%s（该是 %s）",
                    JSON.stringify(got), JSON.stringify(want));
        return false;
      }
      return true;
    };

    // (b) 到上限就加不动。注意闸门看的是**上限**不是屏幕：cap>4 时屏幕只摆 4 个，
    // 那时「＋」是能用的（这一条下面那段就靠它）。
    if (btn.disabled !== (panels.length >= cap)) {
      console.log("FAIL: 「＋」按钮的禁用状态与实际不符（窗口 %d / 上限 %d，disabled=%s）",
                  panels.length, cap, btn.disabled);
      bad1++;
    }
    const n0 = panels.length;
    if (n0 >= cap) {
      const p0 = addPanel();
      if (p0) {
        console.log("FAIL: 窗口已到上限 %d 还能加成第 %d 个", cap, panels.length);
        bad1++;
        removePanel(p0);
      } else {
        // 再**点一下按钮**。真实浏览器对 disabled 的按钮不派发 click，桩会——所以这一下
        // 验的是 `addPanel` 自己那道闸，它才是"上限刚被 /health 改小"那一瞬间唯一站着的。
        btn._listeners["click"].forEach(fn => fn());
        if (panels.length !== n0) {
          console.log("FAIL: 到上限后点「＋」竟然多出一个窗口（%d → %d）", n0, panels.length);
          bad1++;
        }
      }
    }

    // 腾一个位置出来，好让「＋」**真的能加一次**。窗口数 = min(4, 上限)，常见的两种配置
    // （8K 导出 NSESSION=4、32K 导出 NSESSION=2）都是"屏上摆满 = 到上限了"——不先删一个的话，
    // 新加的这条"增删窗口"在最常见的配置下**一次都跑不到**，检查全绿却什么都没验。
    // `panels.length >= 2` 这个前提不能省：NSESSION=1 时页面上只有一个窗口，而"至少留一个"
    // 是**故意**的（全删光之后连单路基线都量不了）。那种配置下这一节本来就无从验起，
    // 要的是明说一句，不是报一个假 FAIL。
    let freed = null;
    if (panels.length < cap) {
      console.log("（上限 %d > 屏幕上的 %d 个窗口，「＋」本来就有空位，不用腾）", cap, panels.length);
    } else if (panels.length < 2) {
      console.log("（上限只有 %d 个会话，页面上只有一个窗口——删了就没法量基线，故意不给删："
                  + "增删这一节跳过）", cap);
    } else {
      freed = panels[panels.length - 1];
      if (!removePanel(freed)) {
        console.log("FAIL: 为了腾位置删最后一个窗口，removePanel 拒绝了（它既不在生成、也不是最后一个）");
        bad1++;
        freed = null;
      } else {
        console.log("腾位置：删掉 %s（剩 %d 个 / 上限 %d）", freed.convId, panels.length, cap);
        if (!namesOk()) { bad1++; }
      }
    }

    // 腾过位置（freed 非空）或本来就有空位（N < cap），这里都应该加得动。
    // 只有 NSESSION=1 那种"屏幕上一个窗口、上限也只有一个"的配置才无从加起，上面已明说。
    const canAdd = panels.length < cap;
    const added = canAdd ? addPanel() : null;
    if (canAdd && !added) {
      console.log("FAIL: 还有空位（现在 %d 个 / 上限 %d）却加不动", panels.length, cap);
      bad1++;
    }
    const slot = added;
    if (slot) {
      console.log("「＋」加成第 %d 个，身份 %s（用过的：%s）",
                  panels.length, slot.convId, JSON.stringify([...usedIds]));
      if (usedIds.has(slot.convId)) {
        console.log("FAIL: 新窗口复用了旧身份 %s —— 网关会把它当成那段旧对话（接着旧历史、命中旧 KV）",
                    slot.convId);
        bad1++;
      }
      usedIds.add(slot.convId);
      if (byId["grid"].children.indexOf(slot.el) < 0) {
        console.log("FAIL: 新窗口没进 #grid（面板数组里有、页面上没有）");
        bad1++;
      }
      if (!namesOk()) { bad1++; }

      // (c) 删窗口要把会话**当场**还回去。先让它真的占住一个会话——不先发一次的话，
      // 后面那次 `waitReleased` 从一开始就是空的，这条检查就成了"空跑也算过"。
      slot.input.value = "删窗口检查";
      await send(slot);
      const mine = (await poolSlots()).filter(s => s.key === "id:" + slot.convId);
      console.log("删之前，%s 占着: %s", slot.convId,
                  JSON.stringify(mine.map(s => "s" + s.session)));
      if (!mine.length) {
        console.log("FAIL: %s 发过一轮却没在 /v1/pool 里占到会话（身份没送出去？）", slot.convId);
        bad1++;
      }
      const gone = slot.convId;
      const nBefore = panels.length;
      if (!removePanel(slot)) {
        console.log("FAIL: removePanel 拒绝了删除（这个窗口既不在生成、也不是最后一个）");
        bad1++;
      } else {
        if (panels.length !== nBefore - 1) {
          console.log("FAIL: 删完窗口数该是 %d，实际 %d", nBefore - 1, panels.length);
          bad1++;
        }
        if (byId["grid"].children.indexOf(slot.el) >= 0) {
          console.log("FAIL: 删掉的窗口还挂在 #grid 上（DOM 没摘，只是数组里没了）");
          bad1++;
        }
        // 交还是 fetch 发出去的（removePanel 没 await 它），所以这里**必须轮询**而不是
        // 立刻读一次：立刻读会把"还没送到"误判成"没还"。要区分的不是几毫秒的时序，
        // 而是"当场还"与"占到 IDLE_TTL（默认 300 秒）超时"——4 秒上限对这条界线绰绰有余。
        const after = await waitReleased();
        if (after.length) {
          console.log("FAIL: 删窗口没把会话还回去，还占着 %s（新窗口要等 IDLE_TTL 才有位置）",
                      JSON.stringify(after));
          bad1++;
        }
        console.log("删掉 %s 后池子里还占着的 web 会话: %s", gone, JSON.stringify(after));
        if (!namesOk()) { bad1++; }

        // 删了再加：新窗口的身份**不能**是刚用过的任何一个。这是这条功能最容易写错的一处——
        // 拿"第几格"当身份的话，删中间那格再补一个，新格子正好捡回旧名字，网关认成同一段
        // 对话（旧历史 + 旧 KV），用户看到的就是"删了没删掉"。
        const again = addPanel();
        console.log("删了再加，新窗口身份 %s", idPane(again));
        if (!again) {
          console.log("FAIL: 删掉一个之后「＋」加不回来了（%d 个 / 上限 %d）", panels.length, cap);
          bad1++;
        } else {
          if (usedIds.has(again.convId)) {
            console.log("FAIL: 新窗口捡回了用过的身份 %s（网关会当成同一段旧对话）", again.convId);
            bad1++;
          }
          usedIds.add(again.convId);
          // 腾过位置就要**把它留着**（窗口数回到原样），没腾过就收回去——两种情况下
          // 这一节跑完的窗口数都必须是 wantPanels，后面那一轮并发检查按这个数比。
          if (!freed) {
            removePanel(again);
            console.log("（没腾位置，把它删回去，恢复 %d 个窗口）", panels.length);
          }
        }
        if (!namesOk()) { bad1++; }
      }
    }

    if (panels.length !== wantPanels) {
      console.log("FAIL: 增删这一节跑完窗口数该是 %d，实际 %d（后面那一轮按这个数比）",
                  wantPanels, panels.length);
      bad1++;
    }
    const leftover = await waitReleased();
    if (leftover.length) {
      console.log("FAIL: 增删窗口这一节跑完还留着 web 会话: %s", JSON.stringify(leftover));
      bad1++;
    }
    if (bad1) {
      console.log("FAIL: 增删窗口有 %d 项不对", bad1);
      process.exit(1);
    }
    console.log("  增删窗口全过（身份不复用 · 到上限加不动 · 删除当场交还会话 · 显示号重排）\n");
  }

  // ---------- 暂停 / 继续：按钮有没有、render 有没有真的拨状态、点下去走哪条路 ----------
  // 「暂停」最怕两种坏法：按钮画了、点了没反应；或者按钮根本没画出来。两种情况下
  // 页面都照跑、看不出坏。render 只负责把 streaming/paused 两个布尔拨到按钮上，
  // 所以这里直接拿一个真面板、手工拨布尔、看按钮的 class 和文字变没变。
  //
  // 2026-09-21 补：只验 render 是不够的。按钮文字会变，但点击处理要是写死走 pauseTurn，
  // 暂停后点「继续」就什么都不会发生（pauseTurn 见到 !streaming 直接 return）——页面
  // 照跑、按钮照变、「继续」形同虚设，全靠人手点才看得出来。所以这里点**真的 click
  // 处理器**，用桩 fetch 记录它打到哪个端点：生成中点击必须打 /v1/stop，暂停后点击
  // 必须打 /v1/chat/completions 且带 continue:true。
  {
    let bad3 = 0;
    const p0 = panels[0];
    if (!p0 || !p0.pause) {
      console.log("FAIL: 面板上没找到暂停按钮（data-role=pause）——HTML 或 querySelector 拼错了");
      bad3++;
    } else {
      // 生成中：按钮亮、文字「暂停」、发送键禁用。
      p0.streaming = true; p0.paused = false;
      ctx.__t.render(p0);
      if (!p0.pause.classList.contains("show")) {
        console.log("FAIL: 生成中暂停按钮没亮出来（缺 .show）"); bad3++;
      }
      if (p0.pause.textContent !== "暂停") {
        console.log("FAIL: 生成中按钮该写「暂停」，实际「" + p0.pause.textContent + "」"); bad3++;
      }
      if (!p0.go.disabled) { console.log("FAIL: 生成中发送键该禁用"); bad3++; }
      // 暂停后：文字「继续」、发送键放开（可以问别的）。
      p0.streaming = false; p0.paused = true;
      ctx.__t.render(p0);
      if (p0.pause.textContent !== "继续") {
        console.log("FAIL: 暂停后按钮该写「继续」，实际「" + p0.pause.textContent + "」"); bad3++;
      }
      if (p0.go.disabled) { console.log("FAIL: 暂停后发送键该放开（可以问别的问题）"); bad3++; }
      // 复位：既没生成、也没暂停，按钮该灭。
      p0.paused = false;
      ctx.__t.render(p0);
      if (p0.pause.classList.contains("show")) { console.log("FAIL: 空闲时暂停按钮该灭掉"); bad3++; }

      // ---- 点击分发：暂停 vs 继续，各打各的端点 ----
      const clickPause = p0.pause._listeners["click"] && p0.pause._listeners["click"][0];
      if (!clickPause) {
        console.log("FAIL: 暂停按钮没有绑定 click 处理器"); bad3++;
      } else {
        const realFetch = ctx.fetch;
        const calls = [];
        ctx.fetch = function (u, o) {
          // /v1/system 透传：continueAnswer 里 sysWake() 会顺手补一拍资源刷新，
          // 打成 503 会把资源条留在"读不到"那一帧，后面的资源条检查就被连带打红。
          if (String(u).indexOf("/v1/system") >= 0) { return realFetch(u, o); }
          let body = null;
          try { body = o && o.body ? JSON.parse(o.body) : null; } catch (e) { body = null; }
          calls.push({ url: String(u), body: body });
          return Promise.resolve({ ok: false, status: 503,
                                   headers: { get: function () { return ""; } },
                                   text: function () { return Promise.resolve("stub"); } });
        };
        try {
          // (a) 生成中点击 = 暂停：必须打 /v1/stop，带上这一轮的 rid。
          calls.length = 0;
          p0.streaming = true; p0.paused = false; p0.requestId = 4242;
          clickPause();
          await new Promise(function (r) { setTimeout(r, 0); });
          if (!calls.some(function (c) { return c.url.indexOf("/v1/stop") >= 0
                                             && c.body && c.body.request_id === 4242; })) {
            console.log("FAIL: 生成中点击「暂停」没打到 /v1/stop（实际 %s）",
                        JSON.stringify(calls.map(function (c) { return c.url; })));
            bad3++;
          }
          // (b) 已暂停点击 = 继续：必须打 /v1/chat/completions 且带 continue:true。
          calls.length = 0;
          const saveMsgs = p0.messages, saveBubble = p0.bubble;
          p0.messages = [{ role: "assistant", content: "半截" }];
          p0.bubble = { textContent: "", parentNode: { className: "" } };
          p0.streaming = false; p0.paused = true; p0.requestId = null;
          clickPause();
          await new Promise(function (r) { setTimeout(r, 0); });
          if (!calls.some(function (c) { return c.url.indexOf("/v1/chat/completions") >= 0
                                             && c.body && c.body.continue === true; })) {
            console.log("FAIL: 暂停后点击「继续」没走 continue 请求（实际 %s）",
                        JSON.stringify(calls.map(function (c) { return c.url; })));
            bad3++;
          }
          if (calls.some(function (c) { return c.url.indexOf("/v1/stop") >= 0; })) {
            console.log("FAIL: 暂停后点击「继续」不该再打 /v1/stop"); bad3++;
          }
          p0.messages = saveMsgs;
          p0.bubble = saveBubble;

          // (c) 「继续」流中途又被暂停（finish_reason=abort）：按钮必须回到「继续」。
          // 这是用户 2026-09-21 报的第二个坑：continueAnswer 的 finally 把 paused 清成
          // false，按钮就此消失、半截回答再也接不回去（只能重新问一句）。这里用一段真的
          // SSE（最后一块带 abort）喂给 continueAnswer，跑完看状态与按钮。
          const saveMsgs2 = p0.messages, saveBubble2 = p0.bubble;
          p0.messages = [{ role: "assistant", content: "半截" }];
          p0.bubble = { textContent: "", parentNode: { className: "" } };
          p0.streaming = false; p0.paused = true; p0.requestId = null;
          const enc = new TextEncoder();
          const sseLines = [
            'data: {"choices":[{"index":0,"delta":{"content":"再"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{"content":"见"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"abort"}]}',
            "data: [DONE]",
          ];
          let sseIdx = 0;
          ctx.fetch = function (u, o) {
            if (String(u).indexOf("/v1/system") >= 0) { return realFetch(u, o); }
            return Promise.resolve({
              ok: true, status: 200,
              headers: { get: function (h) {
                if (h === "X-KV-Reuse") { return "session=0; reset=0; sent=0; base=5; full=5; wait=0"; }
                if (h === "X-Request-Id") { return "77"; }
                return "";
              } },
              body: { getReader: function () {
                return { read: function () {
                  if (sseIdx >= sseLines.length) { return Promise.resolve({ done: true }); }
                  return Promise.resolve({ done: false, value: enc.encode(sseLines[sseIdx++] + "\n") });
                } };
              } },
            });
          };
          clickPause();          // 走 continueAnswer
          for (let i = 0; i < 100 && p0.streaming; i++) {
            await new Promise(function (r) { setTimeout(r, 10); });
          }
          await new Promise(function (r) { setTimeout(r, 0); });
          if (p0.streaming) {
            console.log("FAIL: 「继续」流没有跑完（桩没喂完，或页面卡在 reading）"); bad3++;
          }
          if (!p0.paused) {
            console.log("FAIL: 「继续」中途被暂停后 paused 必须为 true —— 否则按钮消失、回答接不回去");
            bad3++;
          }
          if (!p0.pause.classList.contains("show") || p0.pause.textContent !== "继续") {
            console.log("FAIL: 「继续」中途被暂停后按钮该还在且写「继续」（实际 show=%s text=%s）",
                        p0.pause.classList.contains("show"), p0.pause.textContent);
            bad3++;
          }
          p0.messages = saveMsgs2;
          p0.bubble = saveBubble2;
        } finally {
          ctx.fetch = realFetch;
        }
        // 复位到空闲：后面的检查都假设面板是待机态。
        p0.streaming = false; p0.paused = false; p0.requestId = null;
        ctx.__t.render(p0);
      }
    }
    if (bad3) { console.log("FAIL: 暂停/继续有 %d 项不对", bad3); process.exit(1); }
    console.log("  暂停/继续全过（按钮在 · render 拨「暂停/继续」· 点击分别走 STOP / continue）\n");
  }

  // ---------- 资源条：数字对不对、读不到时怎么说 ----------
  // 用户 2026-09-20 要的"把 RK1828/RK3588 的内存、NPU、CPU、温度动态显示在网页上"。
  // 这里守三条，都是**照跑照显示、屏幕上却不对**的坏法：
  //   a) 缺值不许画成 0 —— 刚打开页面时 NPU 利用率本来就没有（要两次采样才有差值），
  //      画成 0% 等于告诉看的人"板卡闲着"，而那正是最该判断"到底跑没跑起来"的一帧；
  //   b) NPU 利用率是**算出来的**、温度**只有 RK3588 的**，这两句必须写在条子上 ——
  //      RK1828 是 PCIe 设备，主机侧没有 hwmon、SDK 也没有利用率查询接口，不写出来
  //      那两个百分数会被当成硬件读数读走；
  //   c) 后端不答了要当场标旧，不能继续拿上一次的数冒充"现在"。
  //
  // 夹具那一半是必须的：本机（Windows）没有 /proc，RK3588 那半真跑起来永远都是 "—"，
  // 那些格式（GB/MB、每核、°C、开多久）就只能等上了板子靠眼睛看。所以**页面跑的是原
  // 函数**、喂进去的是一份确定的 /v1/system（与 summarize 那组 fixture 同一个套路）。
  {
    let bad2 = 0;
    const liveText = stripHtml(ctx.__t.sysHtml());
    console.log("资源条（本机实跑，RK3588 那半本机读不到）:", liveText.slice(0, 200) || "(空)");
    if (!/RK1828/.test(liveText)) {
      console.log("FAIL: 资源条没在跑（还是占位符？）—— 轮询没起来、或者渲染抛了");
      bad2++;
    }
    if (/正在读取/.test(liveText)) {
      console.log("FAIL: 页面开了这么久资源条还停在「正在读取…」");
      bad2++;
    }
    // 本机的真后端数据（桩后端答的 STAT）：卡数、每卡 NPU/内存两格都得在。
    // 卡数**不跟会话数挂钩**：四张卡就是流水线的四段，一卡一路，会话数是"每卡开几路"。
    // 所以这里跟 /v1/system 自己对——不写死 4，换个配置（或换个 stage 数）也照样成立。
    const sysDocLive = await (await fetch(BASE + "/v1/system")).json();
    const wantCards = ((sysDocLive.npu || {}).cards || []).length;
    const liveCards = (liveText.match(/#\d/g) || []).length;
    if (!wantCards || liveCards !== wantCards) {
      console.log("FAIL: 资源条上画了 %d 张卡，/v1/system 报的是 %d 张", liveCards, wantCards);
      bad2++;
    }

    const SYS_FIX = {
      ts: 1,
      host: {
        cpu: { n: 8, pct: 42, per_core: [10, 20, 30, 40, 50, 60, 70, 80],
               loadavg: [0.51, 0.4, 0.3] },
        mem: { total: 17179869184, available: 6871947673, used: 10307921511,
               used_pct: 60, free: 2000000000 },
        // 板上真有 7 个热区，这里就照着 7 个给（2026-09-20 改）。**顺序 = zone 顺序**、
        // 和板上一样，不是按温度排的——这一份夹具同时钉着"顺序固定"这条：最热的 npu 81°
        // 故意放在**最后**，按温度排的写法会把它挪到第一个，断言立刻红。
        // 三个档都有：big1 68 该是警告色、npu 81 该是危险色、其余是普通色。
        thermal: [{ zone: "thermal_zone0", label: "soc-thermal", c: 45.0 },
                  { zone: "thermal_zone1", label: "bigcore0-thermal", c: 51.2 },
                  { zone: "thermal_zone2", label: "bigcore1-thermal", c: 68.0 },
                  { zone: "thermal_zone3", label: "littlecore-thermal", c: 39.0 },
                  { zone: "thermal_zone4", label: "center-thermal", c: 40.0 },
                  { zone: "thermal_zone5", label: "gpu-thermal", c: 47.0 },
                  { zone: "thermal_zone6", label: "npu-thermal", c: 81.0 }],
        uptime_s: 12345,
      },
      // `temp_age_s` 是**整份卡温文件的年龄**（四张卡同一次采样），不是某一张卡的。
      // 卡温本身在 `cards[i].temp_c`：1 号卡给了 null（那一次没读到），它必须画成 "—"。
      npu: { ok: true, busy_pct: 55.0, temp_age_s: 1.5, cards: [
        { name: "stage0", ctx_len: 32768, run_calls: 12, mem_total: 5319368704,
          mem_free: 500000000, mem_used: 4819368704, mem_used_pct: 90.6, node_num: 8,
          node_min_free: 13 * 1048576, mem_age_s: 1.2, busy_pct: 55.0, temp_c: 41.0 },
        { name: "stage1", ctx_len: 32768, run_calls: 3, mem_total: 5319368704,
          mem_free: 900000000, mem_used: 4419368704, mem_used_pct: 83.1, node_num: 8,
          node_min_free: 0, mem_age_s: null, busy_pct: null, temp_c: null },
      ] },
      backend: { alive: true, sessions: 2, ctx_size: 32768, stats_age_s: 0.4 },
      gateway: { busy: 1, bound: 2, dead: 0, waiting: 0, uptime_s: 3600 },
    };
    const run = (d) => { ctx.__t.renderSys(d); return ctx.__t.sysHtml(); };
    // 温度那一行的格间空格是**不折空格** ` `（普通空格会落在折行点上被吃掉，
    // 屏幕上的"npu 81°· bigcore0"就贴上去了）——读文本的断言先把它看成普通空格。
    const txt = (d) => stripHtml(run(d)).replace(/\u00a0/g, " ")   // 不折空格;
    const chk2 = (ok, msg) => { if (!ok) { console.log("  FAIL: " + msg); bad2++; } };

    const t = txt(SYS_FIX);
    console.log("资源条（夹具）:", t.slice(0, 210));

    // ① RK3588 那半：数字、单位、每核、温度都要真算出来，不是把 JSON 印出来。
    chk2(/CPU 42%/.test(t), "RK3588 的 CPU 占用率没画出来（该是 42%）：" + JSON.stringify(t));
    chk2(/内存 60%/.test(t) && /9\.6 GB \/ 16\.0 GB/.test(t),
         "RK3588 内存该显示「60%」和「9.6 GB / 16.0 GB」：" + JSON.stringify(t));
    // 热区（2026-09-20 改）：**七个全画出来**、**按 zone 顺序**、名字用缩写。
    // 三条各自钉住一件会静默坏掉的事：
    chk2(/soc 45° · big0 51° · big1 68° · little 39° · center 40° · gpu 47° · npu 81°/.test(t),
         "七个热区该全画出来、按 zone 顺序（不是按温度排）、缩写 + 摄氏度、" +
         "格间是文本分隔符：" + JSON.stringify(t));
    // 上面那条**整段**匹配就等于把"顺序固定"钉住了（它要求 soc 在最前、npu 在最后）：
    // 按温度排的写法会把 81° 的 npu 挪到第一个，这条立刻红。所以这里只补一条"不许再有 +N"
    // ——那是上一版的折叠计数，被收起来的四个热区在屏幕上没有任何入口。
    chk2(!/\+\d/.test(t), "不该再有「+N」那种折叠计数（被收起来的热区在屏幕上没有入口）：" +
         JSON.stringify(t));
    // 名字与数字之间必须是**真空格**：写成 flex 的 gap 时屏幕上看着有间隔、复制下来却是 "soc45°"。
    chk2(/<span class="tl">big0<\/span> <b/.test(run(SYS_FIX)),
         "热区名与度数之间该是字面空格（不是 flex/grid 的 gap——那复制下来会贴成 soc45°）：" +
         JSON.stringify(run(SYS_FIX).slice(0, 500)));
    // 缩写而不是截断：`bigcore0` / `bigcore1` 前六个字母一样，截出来的两个"bigcor…"分不清。
    chk2(/<span class="tl">big0<\/span>/.test(run(SYS_FIX)) &&
         /<span class="tl">big1<\/span>/.test(run(SYS_FIX)) &&
         /title="bigcore0-thermal 51°"/.test(run(SYS_FIX)),
         "热区该用能分辨的缩写、且每格的 title 里留全长名字：" +
         JSON.stringify(run(SYS_FIX).slice(0, 500)));
    // 每一格尾巴那个**不折空格**必须是转义写出来的 ` `，不能是普通空格：普通空格
    // 会落在折行点上被吃掉，"soc 45° · big0" 就贴成 "soc 45°· big0"。
    // ⚠️ 这一条**只能查源码/原始 HTML**：下面那个 `txt()` 把不折空格换成普通空格了
    // （为了让文本断言好读），换成普通空格的写法在那里看起来一模一样。
    chk2(/<\/b>\u00a0<\/span><span class="ti"/.test(run(SYS_FIX)),
         "每格尾巴该是不折空格（写在源码里是转义 \\u00a0）——普通空格会在折行点上被吃掉：" +
         JSON.stringify(run(SYS_FIX).slice(0, 500)));
    // 一个都不许丢：title 里那串是"全部热区"的原样清单（全长标签）。
    chk2(/全部热区：soc-thermal 45° · bigcore0-thermal 51° · bigcore1-thermal 68°/.test(run(SYS_FIX)),
         "title 里该有全部热区的全长清单：" + JSON.stringify(run(SYS_FIX).slice(0, 500)));
    chk2(/负载 0\.51/.test(t), "负载没画出来");
    chk2(/开机 3h25m/.test(t), "开机时长该被折成 3h25m：" + JSON.stringify(t));
    chk2((run(SYS_FIX).match(/<i title="[^"]*"><span style="height:/g) || []).length === 8,
         "每核小柱该有 8 根（cpu0..cpu7）");
    chk2(/title="80%"><span style="height:80%"/.test(run(SYS_FIX)),
         "每核小柱的高度必须**就是**占用率（不许给 0% 留底座、也不许统一高度）");

    // ② RK1828 那半：卡号、两格数、最紧 node。
    // `\s+` 而不是单个空格：每一格之间那点空白靠的是 HTML 源码里的空格（`.grp` 是
    // flex，空白不占位），去掉标签之后可能是两个空格——**这里要钉的是"有没有这两格
    // 数、数对不对"，不是空白的个数**。
    // 卡号后面先跟 `ctx 32768`（2026-09-20 改成一块一张卡：一块的头是「RK1828 #0
    // ctx 32768」，把这一路跑着什么上下文长度也写在同一行，省得回头去翻启动参数）。
    chk2(/#0\s+ctx 32768\s+NPU 55%\s+内存 91%/.test(t),
         "0 号卡该显示 #0 / ctx 32768 / NPU 55% / 内存 91%：" + JSON.stringify(t));
    // 卡温（2026-09-20 加）：**一行一卡**，值 + 那一份文件有多旧。
    // 那个年龄是**必须的**——它不是这一秒读的，是板上另一个进程每 5 秒采一次放着的；
    // 不写年龄，屏幕上一个 30 秒前的 41° 和一个刚采的 41° 长得一模一样。
    chk2(/#0\s+ctx 32768\s+NPU 55%\s+内存 91%\s+温度 41° · 1\.5s 前/.test(t),
         "0 号卡该有「温度 41°」和那一份卡温的年代（1.5s 前）：" + JSON.stringify(t));
    // 1 号卡那一次没读到（temp_c 是 null）=> 画 "—"。**不许画 0°**：0° 是一句
    // 肯定的假话（"这张卡凉快"），"—" 才是"没读到"。
    chk2(/#1\s+ctx 32768\s+NPU —\s+内存 83%\s+温度 —/.test(t),
         "没读到的卡温该画「—」而不是 0°：" + JSON.stringify(t));
    chk2(!/温度 0°/.test(t), "卡温缺值被画成了 0°（= 谎报这张卡凉快）");
    // 卡温的颜色档和热区**不一样**（75/85 vs 65/80）：结温是这张卡自己最热的那个点，
    // 比 SoC 里某个采样点更值得早报。夹具里 41° 两档都不该触发。
    chk2(!/<b class="(warn|crit)">41°<\/b>/.test(run(SYS_FIX)),
         "41° 不该是警告色：" + JSON.stringify(run(SYS_FIX).slice(0, 400)));
    // 一张卡一个温度：只改第一张卡的 temp_c，第二张照旧——这样"四张卡共用一个温度"
    // 那种写法（拿 npu 上的一个数往每张卡上抄）会立刻露出来。
    const mkTemp = (c0, age) => Object.assign({}, SYS_FIX, {
      npu: Object.assign({}, SYS_FIX.npu, { temp_age_s: age,
        cards: [Object.assign({}, SYS_FIX.npu.cards[0], { temp_c: c0 }),
                SYS_FIX.npu.cards[1]] }) });
    chk2(/<b class="crit">88°<\/b>/.test(run(mkTemp(88.0, 0.2))),
         "88° 该是危险色（>=85）：" + JSON.stringify(run(mkTemp(88.0, 0.2)).slice(0, 400)));
    chk2(/<b class="warn">75°<\/b>/.test(run(mkTemp(75.0, 0.2))),
         "75° 该是警告色（>=75）：" + JSON.stringify(run(mkTemp(75.0, 0.2)).slice(0, 400)));
    // 70° 是**区分"卡温用了热区那对阈值(65/80)"的那一格**：70 在热区档里是警告色、
    // 在卡温档(75/85)里是普通色。没有这一格的话，把 `tempCls(c.temp_c, 75, 85)` 写成
    // `tempCls(c.temp_c, 65, 80)` 能活下来——上面 75°/88° 那两条在 65/80 下照样绿。
    chk2(/<b class="">70°<\/b>/.test(run(mkTemp(70.0, 0.2))),
         "70° 该是普通色（卡温那一对阈值是 75/85，不是热区的 65/80）：" +
         JSON.stringify(run(mkTemp(70.0, 0.2)).slice(0, 400)));
    // 卡温是**每张卡自己的**：只改第一张卡、第二张还是 null，画面上就该只有一处温度。
    // "拿 npu 上的一个数往每张卡上抄"那种写法（比"读不到"更常见的偷懒）会在这里露出来。
    chk2((txt(mkTemp(88.0, 0.2)).match(/温度 \d+°/g) || []).length === 1,
         "卡温该是每张卡自己的（只改第一张卡，不该四张都变成 88°）：" +
         JSON.stringify(txt(mkTemp(88.0, 0.2))));
    // 采集服务停了（文件不再更新）=> 当场说"已停"，而不是继续拿旧数冒充"现在"。
    // 阈值 20s = 连丢四轮（采集服务 5 秒一轮），**和 SYS_STALE_S(6s) 不是一回事**。
    const oldTemp = txt(Object.assign({}, SYS_FIX, {
      npu: Object.assign({}, SYS_FIX.npu, { temp_age_s: 30.0 }) }));
    chk2(/温度 41° · 已停 30\.0s 前/.test(oldTemp),
         "卡温 30s 没更新就该说「已停」：" + JSON.stringify(oldTemp));
    chk2(new RegExp("TEMP_STALE_S\\s*=\\s*" + ctx.__t.TEMP_STALE_S).test(html) &&
         ctx.__t.TEMP_STALE_S > ctx.__t.SYS_STALE_S,
         "卡温的陈旧阈值该和快照那个分开、且更宽（采集服务 5 秒一轮，拿 6s 去量会一直黄着）：" +
         ctx.__t.TEMP_STALE_S + " vs " + ctx.__t.SYS_STALE_S);
    chk2(/node 13 MB/.test(t), "最紧 node 的余量没画出来（这是最先撞墙的那一格）");
    // "内存 91%" 到底是谁的 91%？**必须写明白**：2026-09-20 第一次上板时后端报的是
    // dev_mem.sys_*（主机侧的小块内存，只有 ~19 MB），页面上就写成了"每卡 19 MB"，
    // 而同一时刻 rknn-smi 报 93%。这一条把"整卡各 node 之和"这句话钉在 title 里。
    chk2(/title="整卡 4\.5 GB \/ 5\.0 GB（各 node 之和），8 个 node"/.test(run(SYS_FIX)),
         "内存那格没把口径写进 title（整卡各 node 之和 vs 主机侧 sys_*）：" +
         JSON.stringify(run(SYS_FIX).slice(0, 500)));
    chk2(!/node 0 MB/.test(t), "node_min_free 为 0 说明**没采到**，不许画成「node 0 MB」");
    chk2(!/node null/.test(t), "读不到的 node 余量漏成了字面的「null」");
    // 没采到就**整格不显示**，不要印一个「node —」出来占位：四张卡各占一格，一格
    // 空着没人会误解，一格写着「—」反倒要人去想"是 0 还是没读到"（0 才是真的危险）。
    chk2(!/node —/.test(t), "没采到的 node 余量被画成了「node —」占位");
    // 内存 90.6% 该是警告色（>=85），不是普通色、也不是危险色（<95）。
    chk2(/<span class="bar warn"><i style="width:90\.6%"><\/i><\/span>/.test(run(SYS_FIX)),
         "内存 90.6% 那条该是警告色：" + JSON.stringify(run(SYS_FIX).slice(0, 400)));
    // 名字挪进了 `.tl` 那一格之后，颜色落在**度数**上（`<b class="crit">81°</b>`），
    // 不再是"npu 81°"整串——所以这条要连 `.tl` 一起判，否则它测的只是"有个 crit 的 b"，
    // 把颜色挂到隔壁 gpu 47° 上照样绿。
    chk2(/<span class="tl">npu<\/span> <b class="crit">81°<\/b>/.test(run(SYS_FIX)),
         "npu 81° 该是危险色（>=80）：" +
         JSON.stringify(run(SYS_FIX).slice(0, 600)));
    chk2(/<span class="tl">big1<\/span> <b class="warn">68°<\/b>/.test(run(SYS_FIX)),
         "big1 68° 该是警告色（>=65）：" + JSON.stringify(run(SYS_FIX).slice(0, 600)));
    chk2(/<span class="tl">soc<\/span> <b class="">45°<\/b>/.test(run(SYS_FIX)),
         "soc 45° 该是普通色（两档都没到）：" + JSON.stringify(run(SYS_FIX).slice(0, 600)));

    // ③ 缺值必须是「—」，**不是 0%**。1 号卡的 busy_pct 是 null（第一次采样还没有差值），
    //    这一格画成 0% 就是"板卡闲着"，而它恰恰是最该判断"跑没跑起来"的那一帧。
    chk2(/#1\s+ctx 32768\s+NPU —\s/.test(t),
         "没采到值的 NPU 该显示「—」而不是 0%：" + JSON.stringify(t));
    chk2(!/NPU 0%/.test(t), "缺值被画成了 0%（= 谎报板卡闲着）");

    // ④ 那两句"别读歪"的话**从可见文字挪进了 tooltip**（2026-09-20，用户要求页面简洁：
    //    "没必要的解释不需要展示在页面上"）。**挪走不等于删掉**——它们说的是屏幕上
    //    **看不出来**的事：NPU 那个百分数是算出来的、两半温度长得一样而来路完全不同。
    //    所以断言从"页面上有这句话"改成"鼠标停上去能看到"，判红的能力一点没少：
    //    谁把 title 删了、或者改短到没说出关键那半句，这几条照样红。
    // ⚠️ 这两个表面要分清楚：`t` 是**剥掉标签之后的可见文字**（"用户看得见什么"就该查它），
    //    `title` 属性当然不在里面；要查提示就得查**原始 HTML**（`run()` 的返回值）。
    const rawFix = run(SYS_FIX);
    chk2(/title="[^"]*卡级忙时占比[^"]*"/.test(rawFix) &&
         /title="[^"]*没有利用率查询接口[^"]*"/.test(rawFix),
         "NPU 那一格没有 tooltip 说明它是算出来的（SDK 没有利用率查询接口）");
    chk2(/title="[^"]*采集服务每 5 秒跑一次 rknn-smi[^"]*"/.test(rawFix),
         "卡温那一格没有 tooltip 说明它的来路（采集服务每 5 秒一次 rknn-smi）："
         + JSON.stringify(rawFix.slice(0, 400)));
    chk2(/title="[^"]*采集服务没装\/没跑时画「—」，不会画 0°[^"]*"/.test(rawFix),
         "卡温的 tooltip 没说明采集服务不在时画「—」而不是 0°");
    chk2(/title="[^"]*这一秒从 \/proc、\/sys 现读[^"]*"/.test(rawFix) &&
         /title="[^"]*主机侧的 hwmon \/ thermal_zone 里没有卡温[^"]*"/.test(rawFix),
         "标题上的 tooltip 没把「两半来路不同」说全：" + JSON.stringify(rawFix.slice(0, 400)));
    // 反向：**可见文字**里不许再有那两段长说明（用户就是嫌它们占地方）。
    chk2(!/生成中每 2 秒刷一次/.test(t) && !/NPU 是卡级忙时占比/.test(t) &&
         !/四张卡的结温由板上/.test(t) && !/主机侧的 hwmon/.test(t),
         "页面上还留着那段可见的长说明：" + JSON.stringify(t.slice(-400)));

    // ⑤ 服务那一格：会话占用、排队、以及**数据多旧**。
    chk2(/会话 2 \/ 2/.test(t) && /排队 0/.test(t), "会话/排队没画出来：" + JSON.stringify(t));
    chk2(/数据 0\.4s 前/.test(t), "没标出这份数据是多久之前采的：" + JSON.stringify(t));
    chk2(!/后端没在答/.test(t), "数据是新的（0.4s）却标了「后端没在答」");

    // ⑥ 后端不答了：当场标旧，而不是继续拿上一次的数冒充"现在"。
    const stale = txt(Object.assign({}, SYS_FIX, {
      backend: { alive: true, sessions: 2, ctx_size: 32768, stats_age_s: 12.0 } }));
    chk2(/数据 12\.0s 前/.test(stale) && /后端没在答/.test(stale),
         "快照 12s 前就该说明白（阈值是 " + ctx.__t.SYS_STALE_S + "s）：" + JSON.stringify(stale));
    const dead = txt(Object.assign({}, SYS_FIX, {
      backend: { alive: false, sessions: 2, ctx_size: 32768, stats_age_s: null },
      npu: { ok: false, cards: [], busy_pct: null } }));
    chk2(/后端 已退出/.test(dead), "后端退出时该显示「已退出」：" + JSON.stringify(dead));
    chk2(/读不到（后端还没答 STAT）/.test(dead), "后端没答 STAT 时该说明白，不能留空");

    // ⑦ 本机/没后端时的退化：不许抛、不许把缺的格子画成 0。
    const emptyDoc = txt({ host: {}, npu: { ok: false, cards: [] },
                           backend: {}, gateway: {} });
    console.log("资源条（空数据）:", emptyDoc.slice(0, 200));
    chk2(/CPU —/.test(emptyDoc), "读不到 CPU 该显示「—」：" + JSON.stringify(emptyDoc));
    chk2(/温度 —/.test(emptyDoc), "读不到热区该显示「—」");
    // 没有 `temp_age_s`（采集服务没装）= 整份文件不在：卡温画 "—"、**而且不许画年龄**
    // （"0.0s 前"会说成"刚刚采的"）。这里连一张卡都没有，所以顺带钉住"没卡也不许抛"。
    const noTempFile = txt(Object.assign({}, SYS_FIX, {
      npu: { ok: true, busy_pct: 55.0, temp_age_s: null,
             cards: [Object.assign({}, SYS_FIX.npu.cards[0], { temp_c: null })] } }));
    chk2(/温度 —/.test(noTempFile) && !/0\.0s 前/.test(noTempFile) &&
         !/已停/.test(noTempFile),
         "采集服务不在时：卡温画「—」，且**不许**编一个「0.0s 前」出来：" +
         JSON.stringify(noTempFile));
    chk2(!/CPU 0%/.test(emptyDoc) && !/内存 0%/.test(emptyDoc),
         "读不到被画成了 0%（0 是假话，缺是另一回事）");
    ctx.__t.renderSysErr("boom");
    const errText = stripHtml(ctx.__t.sysHtml());
    console.log("资源条（读不到）:", errText);
    chk2(/读不到 \/v1\/system/.test(errText) && /boom/.test(errText),
         "拿不到 /v1/system 时该说明白是拿不到：" + JSON.stringify(errText));
    // 走**真那条路**：把 fetch 换成 503，看轮询自己会不会把这一条画出来。
    // 光调 renderSysErr 只能证明"那段文案会渲染"，证不了"抓异常那一段真的接到了它"。
    // 而这条正是最要紧的：观测端点挂了**不许影响这一页的任何功能**（聊天照跑）。
    {
      const realFetch = ctx.fetch;
      ctx.fetch = function () { return Promise.resolve({ ok: false, status: 503 }); };
      try { await ctx.__t.refreshSystem(); } finally { ctx.fetch = realFetch; }
      const viaFetch = stripHtml(ctx.__t.sysHtml());
      console.log("资源条（/v1/system 503）:", viaFetch);
      chk2(/读不到 \/v1\/system/.test(viaFetch) && /503/.test(viaFetch),
           "HTTP 503 时该画出「读不到」而不是抛出去：" + JSON.stringify(viaFetch));
    }

    // ⑧ 切走标签页就停轮询。板卡上这一页是投影看的，没人看的时候多发一个请求都可能
    //    混进正在测量的那几路里去（CPU/带宽都在同一台机器上）。
    ctx.__t.stopSysPoll();
    chk2(ctx.__t.sysOn() === false, "切走之后轮询该停掉");
    ctx.__t.startSysPoll();
    chk2(ctx.__t.sysOn() === true, "切回来该重新开轮询");
    const visListeners = (document._listeners.visibilitychange || []);
    chk2(visListeners.length > 0, "没挂 visibilitychange —— 切走了还在轮询");
    if (visListeners.length) {
      document.hidden = true; visListeners.forEach(f => f());
      chk2(ctx.__t.sysOn() === false, "页面被切走（document.hidden）之后轮询还在跑");
      document.hidden = false; visListeners.forEach(f => f());
      chk2(ctx.__t.sysOn() === true, "切回来没重新开轮询");
    }
    // ⑨ 位置（用户 2026-09-20 要求挪到网页底部）与"这一块有多贵"（要求少占硬件资源）。
    //    位置这一条只能查源码：桩 DOM 是"按 id 现建元素"，没有父子顺序这回事。
    const iSys = html.indexOf('id="sys"'), iGrid = html.indexOf('id="grid"');
    chk2(iSys > 0 && iGrid > 0 && iSys > iGrid,
         "资源那一块该在对话区（#grid）**下面**：" + iGrid + " -> " + iSys);
    chk2(iSys < html.indexOf("</body>"),
         "资源那一块跑到 </body> 外面去了（浏览器会把它丢掉）");

    // 刷新档位：生成中 2s、待机 6s。**这不是两个随手定的数**——这一块每一拍都要网关
    // 读一遍 /proc、/sys，再向后端要一帧 STAT，而四路推理就跑在同一颗 RK3588 上。
    const idleMs = ctx.__t.sysIntervalMs();
    chk2(idleMs === ctx.__t.SYS_POLL_IDLE_MS && idleMs === 6000,
         "待机档该是 6 秒一次：" + idleMs);
    // 用一个假面板去戳"有人在生成"这个条件：真面板此刻是几个、是不是 streaming，
    // 都取决于上面几节跑到了哪儿；拿它当输入的话这条会随别人改而莫名其妙地红/绿。
    panels.push({ streaming: true });
    const busyMs = ctx.__t.sysIntervalMs();
    const busyMs2 = ctx.__t.sysIntervalMs();   // 再问一次：别把 panels 改坏了
    panels.pop();
    chk2(busyMs === ctx.__t.SYS_POLL_BUSY_MS && busyMs === 2000,
         "有人生成时该是 2 秒一次（不然「卡忙起来」要等 6 秒才看得见）：" + busyMs);
    chk2(busyMs2 === 2000, "sysIntervalMs 每次调用都会改状态（应该是纯读）：" + busyMs2);
    chk2(ctx.__t.sysIntervalMs() === 6000, "生成结束该退回待机档");

    // 同一份 doc 不重画。网关侧 1s 快照缓存让"同一秒里后来的看客拿到同一份"成为必然，
    // 而 DOM 重写会让整块重新排版一次——数字一个没变，这一次排版白花。
    // 反过来那半更重要：**ts 变了就必须画**，否则资源条会冻在打开那一帧。
    {
      const realFetch = ctx.fetch;
      // 夹具本身带 `ts: 1`，所以"没有 ts"这一档必须**删掉**这一格——`d.ts = undefined`
      // 是删不掉的（`Object.assign` 复制出来的那一格还在，值也还是 1），那样测的就不是
      // "没有 ts"，而是"ts 没变"。
      const mk = (ts) => function () {
        const d = Object.assign({}, SYS_FIX);
        if (ts === undefined) { delete d.ts; } else { d.ts = ts; }
        return Promise.resolve({ ok: true, status: 200,
                                 json: () => Promise.resolve(d) });
      };
      try {
        ctx.fetch = mk(1000);
        await ctx.__t.refreshSystem();
        const n1 = ctx.__t.sysRenders();
        chk2(ctx.__t.sysLastTs() === 1000, "画完该记住这一份的 ts：" + ctx.__t.sysLastTs());
        ctx.fetch = mk(1000);                    // 同一秒里的第二拍：还是同一份
        await ctx.__t.refreshSystem();
        chk2(ctx.__t.sysRenders() === n1,
             "同一份 doc 又画了一遍（白排一次版）：" + n1 + " -> " + ctx.__t.sysRenders());
        chk2(/CPU 42%/.test(stripHtml(ctx.__t.sysHtml())), "跳过重画不该把内容清掉");
        ctx.fetch = mk(1001);                    // 下一拍：ts 变了
        await ctx.__t.refreshSystem();
        chk2(ctx.__t.sysRenders() === n1 + 1,
             "ts 变了却没重画——资源条会冻在打开那一帧");
        // 不带 ts 的应答（老网关 / 别人自己写的转发）：宁可多画，**不许冻住**。
        ctx.fetch = mk(undefined);
        await ctx.__t.refreshSystem();
        const n2 = ctx.__t.sysRenders();
        await ctx.__t.refreshSystem();
        chk2(ctx.__t.sysRenders() === n2 + 1,
             "应答里没有 ts 时被当成「和上一份一样」了——这一页从此不再刷新");
      } finally { ctx.fetch = realFetch; }
    }

    // ⑩ 高度预算（用户 2026-09-20：对话框下面空一大片，资源块要占底部 1/5~1/4）。
    //    这一条也只能查源码：桩 DOM 没有排版引擎，"屏幕上有没有留白"是浏览器算出来的。
    //    查的是**几块之间的高度关系**，不是具体像素——整页一屏、中间那块吃掉剩下的、
    //    资源块封顶。以前是 `.panel{height:calc(50vh - 60px)}` 加上资源区按内容长，
    //    三块加起来不到一屏，剩下的就是那片空白（用户看到的那片）。
    {
      const styleSrc = html.slice(html.indexOf("<style>") + 7, html.indexOf("</style>"))
        // **注释要剥掉再解析**：上面那些 CSS 注释里恰好写着 `calc(50vh - 60px)`
        // 这种"以前的写法"，不剥的话"不许用 vh 写死高度"这一条会被**注释里的例子**
        // 判红/判绿——判红还好，最坏的一种是它替真代码挡住了检查。
        .replace(/\/\*[\s\S]*?\*\//g, "");
      // 解析成 `{选择器, 声明体, 所在的 @media}`。**必须区分 @media 里的规则**：
      // `.swrap` 在媒体查询里是 `1fr`（退回上下两块），不区分的话"后面那条覆盖前面"
      // 会让正常那两列读成 `1fr`——检查反过来被窄屏的规则骗了。
      const rules = [];
      (function scan(src, media) {
        let i = 0;
        for (;;) {
          const open = src.indexOf("{", i);
          if (open < 0) { return; }
          const sel = src.slice(i, open).trim();
          let depth = 1, j = open + 1;
          while (j < src.length && depth > 0) {
            if (src[j] === "{") { depth++; } else if (src[j] === "}") { depth--; }
            j++;
          }
          const body = src.slice(open + 1, j - 1);
          if (sel.charAt(0) === "@") { scan(body, sel.split(/\s+/)[0] + " " + sel.slice(sel.indexOf("("))); }
          else { rules.push({ sel: sel, body: body, media: media }); }
          i = j;
        }
      })(styleSrc, "");
      // 同一个属性后面的规则覆盖前面的（和 CSS 一致），同一选择器列表里逗号分隔的
      // 每一项都算命中（`html, body { height:100% }` 这一条就是靠它才查得到 body）。
      // 第三个参数给 "@media" 就只查媒体查询里的那一条。
      function decl(sel, prop, media) {
        let out = null;
        rules.forEach(function (r) {
          const want = media ? r.media.indexOf(media) === 0 : r.media === "";
          if (!want) { return; }
          if (r.sel.split(",").map(function (s) { return s.trim(); }).indexOf(sel) < 0) { return; }
          r.body.split(";").forEach(function (d) {
            const i = d.indexOf(":");
            if (i > 0 && d.slice(0, i).trim() === prop) { out = d.slice(i + 1).trim(); }
          });
        });
        return out;
      }

      chk2(decl("html", "height") === "100%" && decl("body", "height") === "100%",
           "整页高度没接成一屏（html 与 body 都要 height:100%）：html=" +
           decl("html", "height") + " body=" + decl("body", "height"));
      chk2(decl("body", "display") === "flex" && decl("body", "flex-direction") === "column",
           "body 该是一列 flex（标题/对话区/资源区依次往下）：" + decl("body", "display") +
           " / " + decl("body", "flex-direction"));
      const mflex = (decl("main", "flex") || "").split(/\s+/);
      chk2(Number(mflex[0]) >= 1 && decl("main", "min-height") === "0",
           "对话区该吃掉标题与资源块之外的全部高度（flex:1 1 auto + min-height:0）：" +
           decl("main", "flex") + " / min-height:" + decl("main", "min-height"));

      // 这一条直接钉那个坑：任何一块用 vh 写死高度，底部就会重新出现一片空白
      // （或反过来，内容被截掉）。**`max-height` 不算**——资源块封顶用的就是它。
      const vhFixed = rules.filter(function (r) {
        return /(^|;)\s*height\s*:\s*[^;]*vh/.test(";" + r.body);
      }).map(function (r) {
        return r.sel + "{" + (r.body.match(/(^|;)\s*height\s*:\s*([^;]*)/) || [])[2] + "}";
      });
      chk2(vhFixed.length === 0,
           "有块用 vh 写死了高度——对话框下面那片空白就是这么来的：" + vhFixed.join(" · "));

      // 上限本身是**矮屏的保险**，不是目标比例：这一块内容实测 249px，1080p 上占 25%、
      // 1440p 上占 17%（用户要的 1/5~1/4 是靠"内容就这么高"实现的）。所以这条只钉住
      // 两头：不许没上限（那会在矮屏上把对话区挤没），也不许小到把内容永远切掉一截。
      const mh = decl(".syssec", "max-height") || "";
      const mv = mh.match(/^(\d+(?:\.\d+)?)vh$/);
      chk2(mv !== null && Number(mv[1]) >= 20 && Number(mv[1]) <= 36,
           "资源块的封顶该落在 1/5~1/3 屏之间（矮屏保险，不是目标比例）：max-height=" + mh);
      chk2(decl(".syssec", "overflow") === "auto",
           "资源块超过上限时该在自己里面滚，而不是把对话区挤扁：" +
           decl(".syssec", "overflow"));

      // 资源块内部是**左右两栏**（左：RK3588 + 服务；右：四张卡）。这一条钉的是 JS 与
      // CSS 的耦合：`swrap`/`sg-host`/`sg-cards` 这三个类名是两边唯一的联系，谁单方面
      // 改了名，页面**照样能跑、数字照样对**——只是退回上下两行，那一块从 249px 长回
      // 376px，底部重新开始挤。上面所有的功能检查都不会红。
      const sysHtmlFix = run(SYS_FIX);
      chk2(/class="swrap"/.test(sysHtmlFix),
           "资源块该有一个 .swrap 包着两栏（没有它两栏就退回上下两行、高 376px）");
      chk2(/class="sgrid sg-host"/.test(sysHtmlFix) &&
           /class="sgrid sg-cards"/.test(sysHtmlFix),
           "两栏的类名该是 sg-host / sg-cards（和 CSS 里那两条 minmax 对不上就散架）");
      chk2(decl(".syssec .sg-cards", "grid-template-columns") !== null &&
           decl(".syssec .sg-host", "grid-template-columns") !== null,
           "CSS 里没有 .sg-host / .sg-cards 的列定义（四张卡会挤成一列）");
      const cols = decl(".syssec .swrap", "grid-template-columns") || "";
      chk2(cols.split("minmax").length - 1 === 2 && decl(".syssec .swrap", "display") === "grid",
           ".swrap 该是两列网格（窄屏那栏由媒体查询管）：" + cols);
      chk2(decl(".syssec .swrap", "grid-template-columns", "@media") === "1fr",
           "窄屏该退回上下两块：媒体查询里没有 `grid-template-columns:1fr` 的话，" +
           "窄窗口下两栏各自都放不下一整行（四张卡会一列排下去）");

      // ---- 2026-09-20 第三次改：七块卡片**一样高、两条底边齐**（用户报"高低不平"）----
      // 等高是**三步叠出来的**，缺任何一步都不齐，而每一步单独看都像是对的：
      //   ① 外层 `.swrap` 拉伸两个子网格 —— 写成 `start` 就退回"各按自己内容长"
      //   ② 子网格 `.sgrid` 的 `align-content` —— **最容易漏**：单行网格在 `start` 下
      //      只占内容高，外层的 stretch 根本传不到卡片上
      //   ③ 卡片 `.sblock` 被拉满这一行（默认 `align-self:stretch`，别写成 flex-start）
      // 这三条**没有任何功能检查能覆盖**：不齐的时候数字全对、会话照还、资源条照刷。
      chk2(decl(".syssec .swrap", "align-items") === "stretch",
           "两栏该等高（.swrap 的 align-items 要 stretch）：写成 start 的话左边三块与" +
           "右边四张卡各按自己内容长，两条底边差 48px（实测 1600 上 771 vs 723）——" +
           "用户报的就是这个。现在是 " + decl(".syssec .swrap", "align-items"));
      chk2(decl(".syssec .sgrid", "align-content") === "stretch",
           "**这一步最容易漏**：单行的网格在 align-content:start 下只占内容高，外层" +
           "stretch 传不到卡片上，七张卡照样不等高。现在是 " +
           decl(".syssec .sgrid", "align-content"));
      chk2((decl(".sblock", "align-self") || "stretch") === "stretch",
           "卡片该被拉到整行高（.sblock 的 align-self 别写成 flex-start）：" +
           decl(".sblock", "align-self"));
      // 等高之后内容是**贴顶**的：齐平的是底边、不是里面的行，内容少的卡片（热区）多出来
      // 的空白全留在底部。不这么做的话七块的"块头 + 第一行数据"就不在一条横线上。
      chk2(decl(".sblock", "display") === "flex" &&
           decl(".sblock", "justify-content") === "flex-start",
           "等高之后内容要贴顶（.sblock 该是 flex 列 + justify-content:flex-start）：" +
           decl(".sblock", "display") + " / " + decl(".sblock", "justify-content"));

      // 列数**写死**，不许 auto-fit / auto-fill：它会在 1500px 以下自己折行——实测
      // 1366 上右栏折成 3+1、1280 上左栏折成 2+1，一折行两组高度就再也对不上，
      // 而且整块反而更高（245→293px）。写死之后卡片只是变窄，永远是一条线。
      chk2(cols.indexOf("auto-fit") < 0 && cols.indexOf("auto-fill") < 0,
           "两栏的列数要写死，不能用 auto-fit / auto-fill（它自己折行、高度立刻对不上）：" + cols);
      chk2((decl(".syssec .sg-host", "grid-template-columns") || "").indexOf("repeat(3") === 0 &&
           (decl(".syssec .sg-cards", "grid-template-columns") || "").indexOf("repeat(4") === 0,
           "左三右四要写死（3:4 正好让七张卡一样宽）：" +
           decl(".syssec .sg-host", "grid-template-columns") + " / " +
           decl(".syssec .sg-cards", "grid-template-columns"));

      // 资源块**不许被压小**。shrink=1（原来是 `flex:0 1 auto`）时，矮屏上多出来的高度
      // 在它和 `main` 之间**按基准尺寸分摊**，于是它掉到比 `max-height` 还小——实测
      // 1366×768 下 249px 的内容只分到 185px，底部那句"NPU 是卡级忙时占比"的说明被切掉，
      // 而那句正是防止屏幕上两个百分数被当成硬件读数读走的。改成 0 之后它的高度只有两种
      // 可能：装得下就按内容、装不下就正好卡在 max-height，要挤全部由 `main` 让。
      const sysFlex = (decl(".syssec", "flex") || "").split(/\s+/);
      chk2(sysFlex[0] === "0" && sysFlex[1] === "0",
           "资源块不该被 flex 压小（flex:0 0 auto）：" + decl(".syssec", "flex"));

      // 「服务」那五行的两列（用户 2026-09-20 点名要的）。和上面 swrap 一样，这一条钉的是
      // JS 与 CSS 的耦合：类名两边唯一的联系，谁单方面改名，页面照跑照对，只是五行又变回
      // 一列、这一块重新变成七块里最高的那块（整条线的高度就由它决定）。
      chk2(/class="svc"/.test(sysHtmlFix),
           "「服务」那一块该包一层 .svc（没有它五行退回一列、这一块重新变成最高的）");
      chk2(decl(".syssec .svc", "display") === "grid" &&
           (decl(".syssec .svc", "grid-template-columns") || "").indexOf("repeat(2") === 0,
           "「服务」该是两列网格：" + decl(".syssec .svc", "grid-template-columns"));
      // 但窄屏要退回一列，而且**两头都有界**。忘了下界（min-width:1181px）的话，1180 以下
      // 又退回上下两组、卡片变回 360px 宽、两列完全放得下，单列白把这一块顶高 48px
      // （实测 217 → 235）。
      const svcMedia = rules.filter(function (r) {
        return r.sel.split(",").map(function (s) { return s.trim(); })
          .indexOf(".syssec .svc") >= 0 && /grid-template-columns\s*:\s*1fr/.test(r.body);
      }).map(function (r) { return r.media; });
      chk2(svcMedia.length === 1 && /max-width:1500px/.test(svcMedia[0]) &&
           /min-width:1181px/.test(svcMedia[0]),
           "「服务」退回一列那条媒体查询要两头都有界（max-width:1500px 且 min-width:1181px）：" +
           JSON.stringify(svcMedia));

      // ---- 宽度不够时的两种"折"（2026-09-20 第三次改的另一半）----
      // 温度那一行是七块里唯一一条**长度不设上界的**：三个热区名 + 度数在 1920 上是 190px，
      // 卡片一窄到 207px 就顶出去了。`.m` 是 nowrap，于是文字**溢出卡片边框、压在隔壁
      // 那张卡上**（实测 1600 / 1366 / 1280 上都是这个症状）。改法不是缩字号，是让它折行。
      chk2(/class="m therm"/.test(sysHtmlFix) && /class="ti"/.test(sysHtmlFix),
           "温度那一行该是会折的 .therm + 不折的 .ti（热区名和它的度数不许被拆开）");
      // 每格自己**不许被拆开**：`.ti` 一旦是 normal，折行就落在"名字"和"度数"中间，
      // 屏幕上会出现一个孤零零的"big0"在行尾、它的 51° 跑到下一行——列表读起来就断了。
      chk2(decl(".syssec .therm .ti", "white-space") === "nowrap",
           "热区每一格该是 nowrap（名字和它的度数永远在一起）：" +
           decl(".syssec .therm .ti", "white-space"));
      chk2(decl(".syssec .m.therm", "display") === "inline-flex" &&
           decl(".syssec .m.therm", "flex-wrap") === "wrap" &&
           (decl(".syssec .m.therm", "flex") || "").indexOf("1 1") === 0,
           "会折的那一行要写成会折的 inline-flex、且拿得到行宽（flex:1 1 auto + min-width:0，" +
           "flex 项默认 min-width:auto 会硬撑到内容宽、撑出边框）：" +
           decl(".syssec .m.therm", "display") + " / " + decl(".syssec .m.therm", "flex"));
      chk2(decl(".syssec .srow", "flex-wrap") === "wrap",
           "资源行要允许折（.srow 的 flex-wrap:wrap）：窄卡片上尾注放不下时，不折行它会" +
           "在**自己内部**一个词一个词地断——实测 1280 上「内存」那一行断成四截、74px 高");
      // 断言要**指到具体那一段**（带字节数/带 title）而不是泛泛地找 `class="m dim2"`：
      // 页面上有三处尾注，泛泛地找的话改坏其中一处、另外两处照样命中，这条就成了空转的。
      chk2(/<span class="m dim2">9\.6 GB \/ 16\.0 GB<\/span>/.test(sysHtmlFix),
           "内存那一行的字节数该标 .m（white-space:nowrap）：光有折行还不够，它得是个" +
           "整体、整段挪到下一行去（否则就是在自己内部断成四截）");
      chk2(/<span class="m dim2" title="全卡最紧那个 node/.test(sysHtmlFix),
           "RK1828 的 node 那一行同理（尾注要整段折，不许断词）");

      // 分隔符必须是**真文本**（写在每一格的开头），不能是 CSS 的 `content:"·"`：生成的内容
      // 复制不出来、读屏软件也时常不念——屏幕上明明是"soc 45° · big0 51°"，复制下来
      // 却是"soc 45°big0 51°"。同一个文件里别处为"标签与值之间那个空格"写过同样的理由。
      // ⚠️ 这一条**只有读页面真跑出来的文本才查得到**（桩 DOM 里 `::after` 根本不生成文本），
      // 所以它跟着夹具那一节那条"格间是文本分隔符"一起成立，这里只判它没被搬回 CSS。
      chk2(/<span class="ti" title="[^"]*">· /.test(sysHtmlFix),
           "温度那一行的分隔符该是**文本**（写在每一格开头），不是 CSS 生成的 content：" +
           "生成的内容复制不出来、读屏软件也时常不念");
      chk2(!/\.therm[^{,]*\{[^}]*content\s*:/.test(styleSrc),
           "别把温度那一行的分隔符搬回 CSS 的 content——那样复制出来就没有分隔符了");
      // 名字那一格（`.tl`）必须**有样式**：七格排在一起，"哪个是哪个"全靠它。没有这条
      // 规则时它继承正文样式、和度数一样重，屏幕上七个热区名和七个数字互相抢注意力。
      chk2(decl(".syssec .therm .tl", "font-size") !== null &&
           decl(".syssec .therm .tl", "color") !== null,
           "热区名那一格（.tl）该有自己的字号与颜色（和度数分开）：" +
           decl(".syssec .therm .tl", "font-size") + " / " +
           decl(".syssec .therm .tl", "color"));
      // 名字与度数之间那个间隔**不许用 gap 去凑**：gap 只是布局上的空隙，屏幕上看着
      // 有间隔、复制下来却会贴成 "soc45°"。所以判的是**真跑出来的 HTML 里那个字面空格**。
      chk2(/<span class="tl">[^<]*<\/span> <b/.test(sysHtmlFix),
           "热区名与度数之间该是字面空格（不是 flex/grid 的 gap——那复制下来会贴成 soc45°）");
      // 卡温那一行**每张卡都要画**，没有读数就画 "—"。少了它，采集服务不在时那一行
      // 整行消失、卡片高度跟着变，屏幕上就是右边一栏在跳。夹具里正好两张卡。
      // `class="lab"` 那一段要连标签一起判：光数「温度」两个字的话，note 里那句
      // "……温度"也会被数进去，两个坏掉一个照样过。
      chk2((sysHtmlFix.match(/class="lab">温度<\/span>/g) || []).length === 2,
           "RK1828 的卡温该是每张卡一行（夹具两张卡该有两行）：" +
           (sysHtmlFix.match(/class="lab">温度<\/span>/g) || []).length + " 行");
      chk2(/<span class="m dim2">· 1\.5s 前<\/span>/.test(sysHtmlFix),
           "卡温那一行的年龄该标 .m（不折）+ .dim2（它是尾注、整段折）：" +
           JSON.stringify(sysHtmlFix.slice(0, 400)));

      // 标题与两条状态行锁住高度。少了这个，长句子会被对话区按 flex-shrink 压扁换行。
      const squeezed = ["header", ".capline", ".sumline"].filter(function (s) {
        return decl(s, "flex") !== "none";
      });
      chk2(squeezed.length === 0, "这几块该锁住高度、不许被压扁（flex:none）：" +
           squeezed.join(" "));
    }

    // 一起跑就补一拍：待机档最长要等 6 秒，"点了②之后卡是不是真的忙了"恰恰是演示时
    // 最想看的那一帧，不能等。
    {
      const realFetch = ctx.fetch;
      let sysCalls = 0;
      ctx.fetch = function (u, o) {
        if (String(u).indexOf("/v1/system") >= 0) { sysCalls++; }
        return realFetch(u, o);
      };
      try {
        ctx.__t.stopSysPoll();
        ctx.__t.startSysPoll();                  // 第一拍立刻发
        await new Promise((r) => setTimeout(r, 120));
        const c1 = sysCalls;
        chk2(c1 >= 1, "开轮询之后第一拍没发出去（calls=" + c1 + "）");
        ctx.__t.sysWake();                       // 生成开始时补的这一拍
        await new Promise((r) => setTimeout(r, 120));
        chk2(sysCalls > c1, "sysWake 没补出这一拍（还在等档位到期）");
      } finally {
        ctx.__t.stopSysPoll();
        ctx.fetch = realFetch;
      }
    }

    // 上面把资源条改成了夹具/错误文案，恢复成真数据，别影响后面那几节（它们会重画页面）。
    ctx.__t.renderSys(null);

    if (bad2) {
      console.log("FAIL: 资源条有 %d 项不对", bad2);
      process.exit(1);
    }
    console.log("  资源条全过（缺值不画 0 · 单位与颜色 · 旧数据标注 · 两半来源说明 · 轮询档位）\n");
    console.log("  布局全过（整页一屏 · 对话区吃掉剩余高度 · 资源块两栏封顶 · 无人写死 vh）\n");
  }

  // 走**用户点「②」的那条路**（runAll），不是自己 `panels.map(send)` 一遍。
  // 差别就在汇总行：自己发的话，页面里 summarize + setSum 那两句压根不执行，
  // 于是"四路那一句怎么算的"完全没有检查覆盖 —— 2026-09-18 改口径时就是这么发现的。
  // 代价是拿不到 runAll 的返回值，得问它要（lastRound）。
  // 上面"删窗口"那一节已经发过请求了，所以下面两处逐字比对的是**这一轮新增的那几条**。
  // 不比整份：整份里混着前面几次探针请求，逐字比对会必然失败，而失败信息又会指向拼错的
  // 方向（"身份不对"）——那是最费时间的一种假警报。
  const idBase = sentIds.length, bodyBase = sentBodies.length;
  await ctx.__t.runAll();
  const round = ctx.__t.lastRound();
  if (!round) { console.log("FAIL: runAll 没留下这一轮的结果"); process.exit(1); }
  const rs = round.rs, wall = round.wall;

  let bad = 0;
  rs.forEach((r, i) => {
    const stat = statOf(panels[i].stat);
    const dot = panels[i].dot.classList.contains("on");
    const active = panels[i].el.classList.contains("active");
    console.log("\n--- 面板 %d ---", i + 1);
    console.log("  返回:", r ? (r.tok + " tok / " + r.el.toFixed(1) + "s = "
                                 + r.rate.toFixed(2) + " tok/s") : "null");
    console.log("  统计行:", stat);
    console.log("  性能小字:", statOf(panels[i].perf) || "(空)");
    console.log("  会话:", panels[i].sess.textContent || "(空)");
    console.log("  答案前 40 字:", JSON.stringify((r ? r.text : "").slice(0, 40)));
    if (!r) { console.log("  FAIL: 这一路没返回"); bad++; return; }
    if (!/tok\/s/.test(stat)) { console.log("  FAIL: 统计行里没有 tok/s（render 把统计行覆盖了？）"); bad++; }
    // 面板头右边那格性能小字（首字延迟 + tok/s）。守的是**结构性**的坏法：`data-role` 拼错、
    // 元素在 innerHTML 里被删掉、setPerf 忘了调 —— 这三种情况下页面照跑、答案照出，只是那格
    // 永远空着，看演示的人根本不会发现少了个数。
    const perf = statOf(panels[i].perf);
    if (!/首字/.test(perf) || !/tok\/s/.test(perf)) {
      console.log("  FAIL: 面板头的性能小字没写出来（首字延迟/tok/s）：" + JSON.stringify(perf));
      bad++;
    }
    if (!/会话/.test(panels[i].sess.textContent)) { console.log("  FAIL: 没拿到会话号"); bad++; }
    if (dot || active) { console.log("  FAIL: 流结束了但状态还是 active"); bad++; }
    if (!r.exact) { console.log("  WARN: usage 没到，token 数是数块数出来的"); }
  });

  // 身份**有没有真的送出去**。这一条是最要紧的：四路匿名时，会话号照样是四个不同值
  // （按首问内容各分一个），四路也照样并发——但占的是四段**匿名**对话，认不出也关不掉，
  // 页面就这么把四个会话全扣在手里，后来的客户端一个都进不来。这个坑真踩过。
  // 期望的身份**从面板自己身上取**，不再写死 web-1..4。窗口能增删之后，写死的那一串
  // 会因为"删过中间一格"而整体错位：那时检查报的是身份错，真正的问题却是检查自己
  // 记着的是旧名单——真东西反而被这行 FAIL 盖住。顺带把"有没有 null / 有没有重复"
  // 一起查了：那才是这一段真正要守的东西（身份没送出去 = 四段匿名对话被扣着）。
  const wantIds = panels.map(p => p.convId);
  const gotIds = sentIds.slice(idBase);
  console.log("\n各路带出去的身份:", JSON.stringify(gotIds),
              "（面板上是", JSON.stringify(wantIds), "）");
  const idsBad = gotIds.length !== wantIds.length
    || gotIds.some(x => !x)                       // 有请求没带身份头
    || JSON.stringify(gotIds) !== JSON.stringify(wantIds)
    || new Set(gotIds).size !== gotIds.length;    // 两路挤进同一段对话
  if (idsBad) {
    console.log("FAIL: 带出去的身份不是 " + JSON.stringify(wantIds)
                + "（匿名对话会把会话扣住不放，后面的客户端全被堵住）");
    // 匿名对话**关不掉名字**（它没有名字），所以这一项失败时池子里会留下几个清不掉的
    // 会话，后面每一次重跑都会撞上它们。把清理办法直接写在这里，省得下一个人从头查。
    console.log("      清理：读 /v1/pool 的 slots[].key（形如 h:...），"
                + "逐个 POST /v1/conversations/close {\"key\": ...}；"
                + "或者用 IDLE_TTL 不为 0 的网关重跑");
    bad++;
  }

  // 输出上限**必须真的跟着请求走**。这一条守的是"输入框接了、但没接线"：页面照跑、
  // 答案照出、四路照样并发，只是长度还停在老的默认值上——从结果上完全看不出来
  //（"模型自己停的"和"上限压根没送出去"长得一模一样）。
  const wantMax = Number(byId["maxtok"].value);
  const gotBodies = sentBodies.slice(bodyBase);
  console.log("\n各路带出去的 max_tokens:",
              JSON.stringify(gotBodies.map(b => (b ? b.max_tokens : null))),
              "（输入框里是 " + wantMax + "）");
  if (!(wantMax > 0)) {
    console.log("FAIL: 输入框里的输出上限不是正数：" + JSON.stringify(byId["maxtok"].value));
    bad++;
  } else if (gotBodies.some(b => !b || b.max_tokens !== wantMax)) {
    console.log("FAIL: 有请求带出去的 max_tokens 与输入框不符（该是 " + wantMax
                + "）——输入框没接线？");
    bad++;
  }

  const sess = panels.map(p => p.kvSession);
  console.log("\n各路落到的会话:", JSON.stringify(sess));
  // 期望值跟着面板数走，不再写死 4：窗口数是由后端上下文容量决定的（32K 只够 2 路），
  // 写死 4 的话那份配置下这条会必然报 FAIL——而真正要守的"每一路各占一个会话、
  // 没有两路挤在一起（挤在一起就是退化成串行）"跟窗口数根本无关。
  if (new Set(sess).size !== panels.length || sess.some(x => x === undefined || x === null)) {
    console.log("FAIL: %d 路没有落在 %d 个不同会话上（会退化成串行！）",
                panels.length, panels.length);
    bad++;
  }
  // 会话池里那四个会话，是不是**记着各自的名字**（光有会话号不够：身份没送出去时
  // 会话号也是四个不同值，但池子里躺着的是四个 `h:...` 摘要）。
  const pool = await (await fetch(BASE + "/v1/pool")).json();
  const holder = {};
  (pool.slots || []).forEach(s => { holder[s.session] = s.key; });
  console.log("跑完这一轮后 /v1/pool 的归属:",
              JSON.stringify((pool.slots || []).map(s => s.session + "=" + s.key)));
  panels.forEach((p, i) => {
    // 期望的 key 从**面板自己的身份**来，不是"第几格"。窗口能增删之后，序号会在删掉中间
    // 一格时整体前移：按序号算的话，被删那一格后面的会话全都"记的是别人的名字"，
    // 检查会报一排身份错，而真正的问题是检查自己拿着过期的名单。
    const got = holder[p.kvSession];
    if (got !== "id:" + p.convId) {
      console.log("  FAIL: 面板 %d 占着 s%s，但池子里记的是 %s（该是 id:%s）",
                  i + 1, p.kvSession, JSON.stringify(got), p.convId);
      bad++;
    }
  });

  // 汇总行：印**页面上真写出来的那一句**（#gsum），再和这里独立算的一遍对一下。
  // 以前这里只是自己重算一个"聚合 tok/s"印出来——那是**检查自己的口径**，页面把它改了
  // 这里也照样印老口径，读日志的人会以为页面还是老样子。现在这里读的是页面产物。
  // 对不上就 FAIL：说明「②」那条路没走 summarize（或者走了别的公式）。
  const okr = rs.filter(Boolean);
  const gsum = byId["gsum"] ? statOf(byId["gsum"]) : "";
  // 比的是**各路速率和**，不是整句：墙钟这一项两边算不出同一个值（页面用 performance.now()，
  // 这里用 Date.now()，还差着一次 await 的往返），拿整句比等于在测时钟抖动。
  // 速率和只用到 tok 与 el —— 那是页面自己量出来、这里直接读回来的同几个对象，应该逐位相等。
  const want = summarize(okr, wall, ctx.__t.baselineRate());
  const soloOf = (s) => { const m = s.match(/速率和 <b>([\d.]+)/); return m ? parseFloat(m[1]) : NaN; };
  console.log("页面汇总行:", gsum.replace(/<[^>]*>/g, "") || "(空)");
  if (!(Math.abs(soloOf(gsum) - soloOf(want)) < 0.005)) {
    console.log("FAIL: 页面汇总行里的速率和与独立算的对不上（%s vs %s）——「②」没走 summarize？",
                soloOf(gsum), soloOf(want));
    bad++;
  }
  if (!/速率和/.test(gsum)) {
    console.log("FAIL: 汇总行里没有「速率和」——伸缩比的分母口径必须写在一句话里");
    bad++;
  }
  console.log("\n统计行渲染后的全局栏:", byId["gstats"].textContent || byId["gstats"].innerHTML);
  // 从这里开始验"页面走了以后，会话有没有还回去"。两条路，代码不是同一段：
  // 关页面走 sendBeacon（beforeunload），点「清空」走 closeConv（fetch）。
  console.log("");

  // 路一：关页面。这是**最容易出事**的一种——演示完顺手关掉标签页，四个会话就被一个
  // 已经消失的页面扣着，直到 IDLE_TTL（默认 300 秒）超时。后来的人全堵在队列里，
  // 而且没有任何地方能看到"是谁占着"（页面都没了）。
  winListeners["beforeunload"].forEach(function (fn) { fn(); });
  const afterUnload = await waitReleased();
  console.log("关页面之后还占着的会话:", JSON.stringify(afterUnload));
  if (afterUnload.length) {
    console.log("FAIL: 关页面没把会话还回去（后来的人要等 IDLE_TTL 超时）");
    bad++;
  }

  // 路二：点「清空」= 这四段对话不要了。再占一次会话来验，否则上一步已经清干净了，
  // 这一条会变成"空跑也算过"。
  await send(panels[0]);
  document.getElementById("clear")._listeners["click"][0]();
  const afterClear = await waitReleased();
  console.log("点「清空」之后还占着的会话:", JSON.stringify(afterClear));
  if (afterClear.length) {
    console.log("FAIL: 清空了但会话没还回去（后来的人要等 IDLE_TTL 超时）");
    bad++;
  }

  // ---- 排队那段等待，页面上必须"会说话" ----
  // 场景：四个会话都被别的对话占着，网关让这一路在队列里等。**这段时间里 fetch 还没
  // resolve**（响应头是拿到会话之后才发的），所以整段时间页面上一个字都不会多——用户
  // 2026-09-18 报的"NPU 明明没人用，输入问题后还是在等"就长这样：屏幕上只有一个转圈，
  // 看不出是模型慢、是排队、还是板卡坏了。
  //
  // 造法不靠真排队（那要占满会话、还得掐时间）：直接把 fetch 换成一个**慢 350ms + 头里
  // 带 wait=42.0** 的假响应。350ms > 页面的 200ms 刷新间隔，所以至少刷一次；头里的
  // wait 是**权威的那个数**（网关自己记的排队时长），比页面 tick 出来的更准。
  {
    const realFetch = ctx.fetch;
    let midWait = null;
    let midPerf = null;
    ctx.fetch = function () {
      return new Promise(function (resolve) {
        setTimeout(function () {
          // fetch 还没回，此刻页面处在"等会话"状态——正是要抓的这一帧
          midWait = statOf(panels[0].stat);
          midPerf = statOf(panels[0].perf);
          resolve({
            ok: true,
            headers: { get: (k) => (k === "X-KV-Reuse"
              ? "session=0; reset=1; sent=204; base=0; full=204; wait=42.0" : null) },
            body: { getReader: () => {
              const chunks = ["data: {\"choices\":[{\"delta\":{\"content\":\"杭\"}}]}\n\n",
                              "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}],"
                              + "\"usage\":{\"completion_tokens\":1}}\n\n",
                              "data: [DONE]\n\n"];
              let i = 0;
              return { read: () => Promise.resolve(
                i < chunks.length ? { done: false, value: new TextEncoder().encode(chunks[i++]) }
                                  : { done: true }) };
            } },
          });
        }, 350);
      });
    };
    try {
      // 上一步「清空」已经把输入框清掉了，而 send() 对着空输入框会直接 return null
      // （不发请求）——不填这一下，这条检查就会变成"空跑也算过"。
      panels[0].input.value = "排队那一格";
      await send(panels[0]);
    } finally {
      ctx.fetch = realFetch;
    }
    const finalStat = statOf(panels[0].stat);
    console.log("\n等待期间的统计行:", midWait);
    console.log("拿到会话后的统计行:", finalStat);
    if (!/等待空闲会话/.test(midWait || "")) {
      console.log("FAIL: 等会话期间统计行没说话（屏幕上只剩转圈，看着像卡死）");
      bad++;
    }
    // 新的一轮开始时，面板头上那格**必须是空的**。留着上一轮的首字延迟/速率，
    // 在最该判断"到底开始了没有"的那一帧上给的是上一轮的数 —— 比空着更坏。
    console.log("等待期间的性能小字:", JSON.stringify(midPerf || ""));
    if (midPerf) {
      console.log("FAIL: 新一轮开始了，面板头上还挂着上一轮的性能数字：" + JSON.stringify(midPerf));
      bad++;
    }
    if (!/排队 42\.0s/.test(finalStat)) {
      console.log("FAIL: 统计行没把排队时长摊出来（tok/s 会把排队算进分母，看着像模型慢）");
      bad++;
    }
  }

  // 放在最后是因为它会**真发一轮请求**（会占一段对话、会往 sentIds 里加东西）。
  // 这一节要钉的是"sysWake 有没有被**接**在生成那条路上"——函数写得好好的、send() 里
  // 忘了调，屏幕上的现象是"点了②之后最多 6 秒里四张卡还都画着闲着"，而那正是演示时
  // 最想看到"卡忙起来"的一帧。前面那几节只证明了 sysWake 自己有用，证不了它被调用。
  {
    const realFetch = ctx.fetch;
    let sysCalls = 0;
    ctx.fetch = function (u, o) {
      if (String(u).indexOf("/v1/system") >= 0) { sysCalls++; }
      return realFetch(u, o);
    };
    try {
      ctx.__t.stopSysPoll();
      await new Promise((r) => setTimeout(r, 50));
      ctx.__t.startSysPoll();                  // 先把轮询开起来（第一拍不等档位）
      await new Promise((r) => setTimeout(r, 150));
      const before = sysCalls;
      panels[0].input.value = "补一拍";         // 空输入框 send() 会直接 return，等于空跑
      await send(panels[0]);
      await new Promise((r) => setTimeout(r, 150));
      if (sysCalls <= before) {
        console.log("FAIL: send() 里没接 sysWake —— 生成起来之后要等 6 秒档位才看得到卡忙"
                    + "（calls " + before + " -> " + sysCalls + "）");
        bad++;
      } else {
        console.log("\n生成开始时补的那一拍: 请求数 " + before + " -> " + sysCalls);
      }
      // 这一轮**真的占了一段对话**，用完就还（和页面里点「×」走同一条路）。
      // 不还的话：板上默认 IDLE_TTL 300s，下一个人要等到超时才有位置；而对本地桩
      // 网关（`--idle-ttl 0` = 不回收）就是**永久占着**——同一个桩网关连跑两遍
      // page_check，第二遍会在"增删窗口"那一节直接红，看着像页面坏了。
      if (!removePanel(panels[0])) {
        console.log("  WARN: 这一轮占的会话没能交还（removePanel 拒绝了），桩网关连跑时会串味");
      } else {
        // 交还是**异步**发出去的（removePanel 不 await 它），而这一节之后脚本马上就要
        // `process.exit`——不等这一下，那个会话就留在池子里了：板上占着 IDLE_TTL（默认
        // 300s），本地桩网关（`--idle-ttl 0` = 不回收）则是**永久**占着。
        await new Promise((r) => setTimeout(r, 300));
      }
    } finally {
      ctx.__t.stopSysPoll();
      ctx.fetch = realFetch;
    }
  }

  // 页面要过、但 HTML 里不存在的 id。上面每次命中都会打一行 FAIL，这里补一句汇总，
  // 顺带把"拼错的到底是哪个"钉在最显眼的位置（页面拼错 id 时多半会紧接着 TypeError，
  // 堆栈会把前面那几行冲掉）。
  if (missingIds.length) {
    console.log("\n页面问过的、HTML 里不存在的 id: " + JSON.stringify(missingIds));
    bad += missingIds.length;
  }

  console.log(bad ? "\n===== 有 " + bad + " 项 FAIL =====" : "\n===== 全部通过 =====");
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error("炸了:", e); process.exit(2); });
