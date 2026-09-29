#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 RK1828 四张卡的结温从 `rknn-smi` 里抠出来，写成一个文件给网关读。

**为什么要有这么一个独立的、以 root 跑的小进程**（2026-09-20 加）：

  · 卡温只有 `rknn-smi` 拿得到。SDK 的查询枚举（`rknn3_api.h` 的 `RKNN3_QUERY_*`）
    里根本没有温度项；主机侧的 sysfs 也没有——四张卡是 PCIe 设备（驱动 `rkep`），
    `/sys/class/hwmon` 下那 9 个读数**全是 RK3588 自己的**（hwmon0~6 就是
    thermal_zone0~6 的别名）。真正能读到卡温的是 `rknn-smi`，它走 PCIe 直接问卡：
    `rknn-smi info -t temp` → Device 0~3 各有 `Chip Temperature (C)`。
  · 它**必须 root**：`/dev/pcie-rkep-0000:01:00.0` 这几个字符设备是
    `crw------- root root`，`pi` 打不开。非 root 跑 rknn-smi 是**静默**失败
    （rc=255，一个字都不打印），看不出是权限问题。
  · 网关**不提权**。它是局域网上无鉴权的常驻服务，给它开一条免密 sudo 等于把
    "能拉起 root 进程"变成它能做的事。所以拆成两半：这一半以 root 跑、
    **只会读、只写 /run 下一个文件**；网关还是 `pi`，只读那个文件。
  · ⚠️ **退出码不可信**：这块板上的 rknn-smi **成功时也返回 255**（root 身份跑
    `info -t temp` / `info -t soc`，表打得完完整整，rc 一律 255，stderr 也空）。
    而非 root 失败同样是 255，区别只在 **stdout 0 字节**。判据只能是"有没有吐出
    东西"，详见 `judge_smi_result()` —— 第一版按 rc 判，真机上每轮都"采样失败"。
  · ⚠️ **同一时刻只能有一个 rknn-smi 在跑**。板上若残留着守护式的
    `rknn-smi info -w`，之后新起的任何一个都会 `Failed to initialize rknnsmi`——
    2026-09-20 就是被这个挡住的，差点又得出"卡温查不到"的结论。
    所以这里每次都是**一次性调用**（不是 `-w`），并且失败时**不覆盖**上一次的
    好值：文件里的 `ts` 会让年龄继续涨，页面据此说"这个数有多旧"。

一次采样 ≈ 135ms（板上实测，root 身份、不含 sudo 的 PAM）。5 秒一次 ≈ 单核 2.7%。
推理侧实测无影响：同样 400 token，不轮询 35.3s、每 2s 轮询一次 33.9s（噪声内）。

输出文件（**原子替换**，读者永远看到完整的 JSON）：

    {"ts": <epoch 秒>, "devices": [{"id": 0, "c": 28.0}, {"id": 1, "c": 35.0}, ...]}

`c` 为 null = 这一次没读到（设备离线 / `NA`），**不是 0**——页面上要画 "—"。

用法：

    sudo ./rknn_smi_temp.py --once      # 采一次就退（排障用）
    sudo ./rknn_smi_temp.py             # 常驻，默认 5 秒一次
    ./rknn_smi_temp.py --selftest       # 本机跑解析器（不要板卡、不要 root）
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

DEFAULT_OUT = "/run/rknn-temp.json"
DEFAULT_SMI = "/usr/bin/rknn-smi"
DEFAULT_INTERVAL = 5.0
SMI_TIMEOUT = 8.0        # rknn-smi 正常 0.2s；卡住了也不能把这一轮拖死


def _temp(s):
    """`"28"` -> 28.0；`"NA"` / 空 / `"nan"` / 离谱值 -> None（= 没读到，不是 0）。"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if v != v or v <= -100.0 or v >= 200.0:   # nan 或明显不是摄氏度
        return None
    return round(v, 1)


def parse_temp_output(text):
    """`rknn-smi info -t temp` 的文本 -> [{"id": 0, "c": 28.0}, ...]。

    实测格式（`cat -A` 确认过，冒号前那串空格是固定的）：

        Device ID                     : 0
        Device Temperature (C)        : NA
        Chip Count                    : 1
          Chip ID                       : 0
          Chip Temperature (C)          : 28

    两个键长得像、**必须分清楚**：`Device Temperature` 是整卡的（这块板上一律 `NA`，
    卡上没装那个传感器），`Chip Temperature` 是芯片结温（有数）。只按
    `Temperature (C)` 去匹配会把两者混在一起，`NA` 那一行会顶掉真值。

    一张卡可能有多个 chip（`Chip Count > 1`），这时取**最热的那个**——屏幕上看
    "这张卡烫不烫"要的是最坏值。一个 chip 都没有（或全是 NA）才退回整卡温度。
    """
    devices = []
    cur = None
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if key == "Device ID":
            try:
                did = int(val)
            except ValueError:
                did = None
            cur = {"id": did, "device_c": None, "chips": []}
            devices.append(cur)
        elif cur is not None and key == "Device Temperature (C)":
            cur["device_c"] = _temp(val)
        elif cur is not None and key == "Chip Temperature (C)":
            t = _temp(val)
            if t is not None:
                cur["chips"].append(t)

    out = []
    for d in devices:
        if d["chips"]:
            c = max(d["chips"])
        else:
            c = d["device_c"]
        out.append({"id": d["id"], "c": c})
    return out


def judge_smi_result(returncode, text, err):
    """把一次 rknn-smi 的结果翻成"成功 / 为什么失败"。**退出码只作参考，不作判据。**

    ⚠️ 2026-09-20 板上实测：`rknn-smi` **成功时也返回 255**——root 身份跑
    `info -t temp` 和 `info -t soc`，表打得完完整整，rc 一律 255（stderr 也是空的）。
    而**非 root** 跑是 rc=255 **且 stdout 0 字节**（权限不够，它静默地什么都不说）。
    两者退出码一模一样，唯一的区别是**有没有吐出东西**。

    所以判据只能是"有没有输出"+"能不能解析出 Device 段"：
      · 按 `rc != 0` 报错 ⇒ 真机上每一轮都"采样失败"，而失败时脚本**不覆盖旧值**
        ⇒ 文件永远不写 ⇒ 页面上四张卡永远是 "—"；偏偏手敲 `sudo rknn-smi info -t temp`
        又明明有数。这是最难查的那一类"脚本和手敲不一致"。
      · 反过来，真失败（设备忙、非 root、格式变了）恰好都不吐表，这套判据照样能报出来。
    """
    err = (err or "").strip()
    tail = ("：%s" % err[:200]) if err else ""
    if not (text or "").strip():
        # 非 root 就是这么失败的。单独说一句，省得下一个人又去翻解析器。
        raise RuntimeError("rknn-smi 没有任何输出（rc=%d%s；非 root 跑就是这样静默失败）"
                           % (returncode, tail))
    devs = parse_temp_output(text)
    if not devs:
        raise RuntimeError("rknn-smi 的输出里没有 Device ID 段（rc=%d%s），格式变了吗？"
                           % (returncode, tail))
    return devs


def read_temps(smi=DEFAULT_SMI):
    """跑一次 rknn-smi，返回 [{"id": .., "c": ..}]。失败抛异常（调用点决定要不要覆盖）。"""
    p = subprocess.run([smi, "info", "-t", "temp"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=SMI_TIMEOUT)
    return judge_smi_result(p.returncode,
                            (p.stdout or b"").decode("utf-8", "replace"),
                            (p.stderr or b"").decode("utf-8", "replace"))


def write_atomic(path, obj):
    """先写临时文件再 `os.replace`：读者不会读到写了一半的 JSON。

    chmod 0644 是**必须的**：`mkstemp` 建出来的是 0600，而读它的网关是 `pi`
    （不是 root）——不改权限的话文件在、内容对，网关却永远 `Permission denied`，
    页面上就是一直 "—"。这类"写得成、读不到"的坑不写下来下一个人还会踩。
    """
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rknn-temp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sample_once(smi, out, log=sys.stderr):
    """采一次并写文件。成功返回表，失败返回 None（**文件原样不动**）。"""
    try:
        devs = read_temps(smi)
    except Exception as e:                      # noqa: BLE001 —— 观测脚本不该因它退出
        log.write("[rknn-temp] 采样失败（保留上一次的值）：%s\n" % (e,))
        log.flush()
        return None
    try:
        write_atomic(out, {"ts": round(time.time(), 3), "devices": devs})
    except OSError as e:
        log.write("[rknn-temp] 写 %s 失败：%s\n" % (out, e))
        log.flush()
        return None
    return devs


def main(argv=None):
    ap = argparse.ArgumentParser(description="RK1828 结温采集（需 root）")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON（默认 %(default)s）")
    ap.add_argument("--smi", default=None, help="rknn-smi 路径（默认自动找）")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="常驻模式下的采样间隔秒（默认 %(default)s）")
    ap.add_argument("--once", action="store_true", help="采一次就退出")
    ap.add_argument("--selftest", action="store_true", help="只跑解析器（不要板卡/root）")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    smi = a.smi or shutil.which("rknn-smi") or DEFAULT_SMI
    if not os.path.exists(smi):
        sys.stderr.write("[rknn-temp] 找不到 rknn-smi（%s）\n" % smi)
        return 2

    if a.once:
        return 0 if sample_once(smi, a.out) is not None else 1

    # 常驻：失败**不退出**（设备忙、别人也在跑 rknn-smi 都会失败，下一轮多半就好），
    # 但把失败频率压下来，别把 journal 刷满。
    last_err = 0.0
    while True:
        t0 = time.time()
        devs = sample_once(smi, a.out)
        if devs is None:
            now = time.time()
            if now - last_err >= 60.0:
                last_err = now
        sleep = a.interval - (time.time() - t0)
        time.sleep(max(0.2, sleep))


# ---------------------------------------------------------------------------
# 解析器的自检：喂几段文本进去，看它认不认得出该认的、跳不跳得掉该跳的。
# 本机就能跑（不需要板卡、不需要 root），所以解析规则改了这里先红。
# ---------------------------------------------------------------------------
def selftest():
    ok = [0, 0]

    def check(name, got, want):
        good = (got == want)
        ok[0 if good else 1] += 1
        print("%s %s" % ("PASS" if good else "FAIL", name))
        if not good:
            print("     期望 %r\n     实得 %r" % (want, got))

    # 板上原样抓下来的（`cat -A` 确认过缩进）。Device 温度一律 NA，Chip 有数。
    real = (
        "  Device ID                     : 0\n"
        "  Device Temperature (C)        : NA\n"
        "  Chip Count                    : 1\n"
        "    Chip ID                       : 0\n"
        "    Chip Temperature (C)          : 28\n"
        "\n"
        "  Device ID                     : 1\n"
        "  Device Temperature (C)        : NA\n"
        "  Chip Count                    : 1\n"
        "    Chip ID                       : 0\n"
        "    Chip Temperature (C)          : 35\n"
    )
    check("板上真实输出", parse_temp_output(real),
          [{"id": 0, "c": 28.0}, {"id": 1, "c": 35.0}])

    # ⚠️ 这一条是这套解析最容易写错的地方：`Device Temperature (C)` 和
    # `Chip Temperature (C)` 都含 `Temperature (C)`，只按后半截匹配的话
    # 第一个 NA 会把它自己的真值顶掉（两张卡都变 None）。
    check("Device 的 NA 不许顶掉 Chip 的真值",
          [d["c"] for d in parse_temp_output(real)], [28.0, 35.0])

    # 整卡温度有数、chip 没有 -> 用整卡的
    fallback = ("  Device ID : 2\n  Device Temperature (C) : 41\n"
                "  Chip Count : 1\n    Chip Temperature (C) : NA\n")
    check("chip 是 NA 时退回整卡温度", parse_temp_output(fallback), [{"id": 2, "c": 41.0}])

    # 一卡多 chip -> 取最热的
    multi = ("  Device ID : 0\n  Device Temperature (C) : NA\n  Chip Count : 2\n"
             "    Chip Temperature (C) : 30\n    Chip Temperature (C) : 47\n")
    check("多 chip 取最热", parse_temp_output(multi), [{"id": 0, "c": 47.0}])

    # 全 NA -> None（页面要画 "—"），不是 0
    na = "  Device ID : 3\n  Device Temperature (C) : NA\n  Chip Temperature (C) : NA\n"
    check("全是 NA 是 None 不是 0", parse_temp_output(na), [{"id": 3, "c": None}])

    # 别的子命令的输出混进来也不许认错：`rknn-smi info` 那张表里有 `Temp(C)` 列头
    table = ("| 0             Online   | OK            | NA            | 0 |\n"
             "| 0             RK1828   | 0000:01:00.0  | 24            | 4714 / 5120 |\n")
    check("info 表格不被误读", parse_temp_output(table), [])

    # 空输入 / 设备离线（整段没有 Device ID）-> 空表，调用点据此报错而不是写空文件
    check("空输入 -> 空表", parse_temp_output(""), [])
    check("没有 Device ID 段 -> 空表", parse_temp_output("  something : else\n"), [])

    # 温度范围过滤：-273 那种明显不是摄氏度的值当没读到
    check("离谱值当没读到", _temp("-273"), None)
    check("普通值是 float", _temp("28"), 28.0)

    # ⚠️ **退出码不能当判据**：板上 rknn-smi 成功时也返回 255。这一条是这轮唯一
    # 靠"检查全绿"抓不出来、只能靠真机跑出来的缺陷（本地 10 PASS 的时候它就在那儿）。
    check("rc=255 但吐了表 = 成功", judge_smi_result(255, real, ""),
          [{"id": 0, "c": 28.0}, {"id": 1, "c": 35.0}])
    check("rc=0 有表 = 成功", judge_smi_result(0, real, ""),
          [{"id": 0, "c": 28.0}, {"id": 1, "c": 35.0}])
    silent = ""
    try:
        judge_smi_result(255, "", "")
    except RuntimeError:
        silent = "RuntimeError"
    check("rc=255 且 0 字节 = 失败（非 root 的样子）", silent, "RuntimeError")
    garbage = ""
    try:
        judge_smi_result(0, "  something : else\n", "")
    except RuntimeError:
        garbage = "RuntimeError"
    check("有输出但没有 Device 段 = 失败", garbage, "RuntimeError")

    print("\n解析器自检：%d PASS / %d FAIL" % (ok[0], ok[1]))
    return 0 if ok[1] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
