#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三个改动的收益评测：A 窗口滚动 / B 削冗余 prompt / C 会话换页。

默认自动拉起「网关 + 桩后端」（无需板卡，Windows/Linux 都能跑），逐项做开/关对比：
  · A：同一段超长历史，窗口关闭时会 HTTP 400（上下文满），开启时被裁剪到预算内 → 200；
  · B：超长 tool 结果，关闭截断时渲染出的 prompt 字节数 vs 截断后；工具顺序漂移是否还会
       导致粘性前缀失效（reset=1）；
  · C：开启 --kv-swap-dir 后，空闲回收会把 KV 存盘（/v1/pool 的 swapped/swap_out/swap_in），
       同一段对话回来时读回、按前缀复用（X-KV-Reuse reset=0）。

对真板卡黑盒测量（只读指标）：
    python tools/benefit_eval.py --base http://<板卡IP>:18280
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATEWAY_DIR = os.path.join(ROOT, "gateway")
GATEWAY = os.path.join(GATEWAY_DIR, "rkllm_gateway.py")
FAKE = os.path.join(ROOT, "tests", "fake_backend.py")
CTX = 4000
SESSIONS = 2


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def parse_kv(headers):
    v = headers.get("X-KV-Reuse")
    if not v:
        return {}
    out = {}
    for part in v.split(";"):
        k, _, val = part.strip().partition("=")
        out[k] = val
    return out


class GW(object):
    """拉起一个网关（+桩后端）并暴露极简 HTTP 客户端。"""

    def __init__(self, extra_gw=(), ctx_limit=CTX, sessions=SESSIONS,
                 idle_ttl=300.0, kv_swap_dir=None, backend_extra=()):
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        cmd = [sys.executable, GATEWAY, "--port", str(self.port), "--frames-stdout",
               "--sessions", str(sessions), "--idle-ttl", str(idle_ttl),
               "--contend-idle", "0", "--queue-timeout", "60", "--verbose"]
        cmd += list(extra_gw)
        if kv_swap_dir:
            cmd += ["--kv-swap-dir", kv_swap_dir]
        cmd += ["--", sys.executable, FAKE, "--sessions", str(sessions),
                "--ctx-limit", str(ctx_limit), "--ctx-size", str(ctx_limit),
                "--delay", "0.02"] + list(backend_extra)
        self.logf = tempfile.NamedTemporaryFile(prefix="benefit-gw-", suffix=".log",
                                                delete=False)
        self.proc = subprocess.Popen(cmd, cwd=GATEWAY_DIR, stdout=subprocess.DEVNULL,
                                     stderr=self.logf)
        self._wait()

    def _wait(self):
        for _ in range(300):
            try:
                self.get("/health", timeout=1)
                return
            except Exception:                              # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("gateway did not start; see %s" % self.logf.name)

    def get(self, path, timeout=30):
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)

    def chat(self, messages, timeout=90, **body):
        payload = {"messages": messages, "stream": False}
        payload.update(body)
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw), dict(e.headers)
            except ValueError:
                return e.code, {"raw": raw}, dict(e.headers)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:                                  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:                              # noqa: BLE001
                pass
        self.logf.close()


def build_history(pairs, size=220):
    msgs = [{"role": "system", "content": "S" * 300}]
    for i in range(pairs):
        msgs.append({"role": "user", "content": ("u%d " % i) + "x" * size})
        msgs.append({"role": "assistant", "content": ("a%d " % i) + "y" * size})
    return msgs


def exp_a():
    """A：历史逐轮增长，窗口关闭 vs 开启 的成功/失败。

    必须像真实客户端那样把**模型上一轮的原样输出**作为 assistant 消息回显，粘性前缀才
    成立、后端 ctx 才会真正累积到溢出；否则每轮都 RESET，桩根本到不了"上下文满"。
    """
    print("\n=== 改动 A：单会话在有界内存内滚动 ===")
    out = {}
    for flags, label in ((["--no-window"], "关闭窗口"), ([], "开启窗口")):
        gw = GW(extra_gw=flags)
        try:
            ok = bad = 0
            msgs = [{"role": "system", "content": "S" * 200},
                    {"role": "user", "content": "开始"}]
            for t in range(12):
                msgs = msgs + [{"role": "user", "content": "问题 %d " % t + "z" * 3000}]
                code, resp, hdr = gw.chat(msgs, max_tokens=16, conversation_id="convA")
                if code == 200:
                    ok += 1
                    ans = resp["choices"][0]["message"]["content"]
                else:
                    bad += 1
                    ans = "(failed)"
                msgs = msgs + [{"role": "assistant", "content": ans}]
            out[label] = (ok, bad)
            print("  %s：成功 %d 轮 / 失败 %d 轮" % (label, ok, bad))
        finally:
            gw.stop()
    gain = out["开启窗口"][0] - out["关闭窗口"][0]
    print("  => 开启窗口多成功 %d 轮（失败从 %d 降到 %d）"
          % (gain, out["关闭窗口"][1], out["开启窗口"][1]))
    return out


def exp_b():
    """B：超长 tool 结果截断 + 工具顺序漂移。"""
    print("\n=== 改动 B：削冗余 prompt ===")
    big = "file content: " + ("Lorem ipsum dolor sit amet " * 4000)
    msgs = [{"role": "user", "content": "read it"},
            {"role": "tool", "content": big}]
    sizes = {}
    for flags, label in ((["--tool-output-max-bytes", "0"], "不截断"), ([], "截断8KB")):
        gw = GW(extra_gw=flags)
        try:
            code, resp, hdr = gw.chat(msgs, max_tokens=8, conversation_id="convB")
            sizes[label] = int(parse_kv(hdr).get("full", 0))
            print("  %s：渲染 prompt %d 字节（HTTP %d）" % (label, sizes[label], code))
        finally:
            gw.stop()
    if sizes.get("不截断"):
        print("  => 截断后 prompt 缩小 %.1f%%"
              % (100.0 * (1 - sizes["截断8KB"] / float(sizes["不截断"]))))

    # 工具顺序漂移是否还会让前缀失效
    def tooldef(name):
        return {"type": "function",
                "function": {"name": name, "description": name + " tool",
                             "parameters": {"type": "object", "properties": {}}}}
    gw = GW()
    try:
        m1 = [{"role": "user", "content": "hello"}]
        _, r1, _ = gw.chat(m1, tools=[tooldef("beta"), tooldef("alpha")],
                           conversation_id="convT", max_tokens=8)
        ans = r1["choices"][0]["message"]["content"]
        m2 = m1 + [{"role": "assistant", "content": ans},
                   {"role": "user", "content": "again"}]
        _, r2, h2 = gw.chat(m2, tools=[tooldef("alpha"), tooldef("beta")],
                            conversation_id="convT", max_tokens=8)
        kv = parse_kv(h2)
        print("  工具顺序调换后 reset=%s base=%s（reset=0 表示前缀仍命中）"
              % (kv.get("reset"), kv.get("base")))
    finally:
        gw.stop()


def exp_c():
    """C：会话换页（回收存盘、回来读回复用）。"""
    print("\n=== 改动 C：会话换页（save/load_kvcache） ===")
    kv_dir = tempfile.mkdtemp(prefix="kvswap-eval-")
    gw = GW(idle_ttl=1.0, kv_swap_dir=kv_dir)
    try:
        _, ra, _ = gw.chat([{"role": "user", "content": "alice first question"}],
                           conversation_id="A", max_tokens=8)
        a1 = ra["choices"][0]["message"]["content"]
        gw.chat([{"role": "user", "content": "bob first question"}],
                conversation_id="B", max_tokens=8)
        time.sleep(1.6)
        gw.chat([{"role": "user", "content": "carol first question"}],
                conversation_id="C", max_tokens=8)          # 触发回收 A
        pool, _ = gw.get("/v1/pool")
        print("  回收后 /v1/pool：swapped=%s swap_out=%s swap_in=%s"
              % (pool.get("swapped"), pool.get("swap_out_total"),
                 pool.get("swap_in_total")))
        time.sleep(1.6)                                     # 让 B/C 也可回收
        _, rb, hb = gw.chat(
            [{"role": "user", "content": "alice first question"},
             {"role": "assistant", "content": a1},
             {"role": "user", "content": "alice second question"}],
            conversation_id="A", max_tokens=8)
        kv = parse_kv(hb)
        pool2, _ = gw.get("/v1/pool")
        print("  A 回来：reset=%s base=%s（reset=0/base>0 表示读回了 KV）"
              % (kv.get("reset"), kv.get("base")))
        print("  最终 /v1/pool：swapped=%s swap_out=%s swap_in=%s"
              % (pool2.get("swapped"), pool2.get("swap_out_total"),
                 pool2.get("swap_in_total")))
    finally:
        gw.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None,
                    help="对已运行的网关只读测量（跳过自动拉起与 A/B/C 构造）")
    ap.add_argument("--only", default="abc", help="只跑哪几项，如 ab / c")
    args = ap.parse_args()
    if args.base:
        print("黑盒测量 %s" % args.base)
        pool, _ = urllib.request.urlopen(args.base + "/v1/pool", timeout=10), None
        doc = json.loads(pool.read().decode("utf-8"))
        print("/v1/pool: %s" % json.dumps(
            {k: doc.get(k) for k in ("sessions", "reaped_total", "swapped",
                                     "swap_out_total", "swap_in_total")},
            ensure_ascii=False))
        return 0
    if "a" in args.only:
        exp_a()
    if "b" in args.only:
        exp_b()
    if "c" in args.only:
        exp_c()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
