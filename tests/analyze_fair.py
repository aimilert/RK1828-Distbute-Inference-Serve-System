#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""④ 公平性：从 bench_agent 结果 JSON 计算"每对话延迟"的分布与 Gini 系数。

Gini=0 完全公平（各对话延迟相同）；越大越不公。用于比较不同调度策略下的公平性。
"""
from __future__ import annotations

import glob
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def gini(xs):
    xs = sorted(float(x) for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    s = sum(xs)
    if s <= 0:
        return 0.0
    cum = sum((i + 1) * v for i, v in enumerate(xs))
    return (2.0 * cum - (n + 1) * s) / (n * s)


def per_conv(records):
    agg = {}
    for r in records:
        k = r.get("conversation")
        if r.get("ok") is False:
            continue
        agg.setdefault(k, []).append(r.get("e2e_ms"))
    return {k: sum(v) for k, v in agg.items()}


def main(paths):
    rows = []
    for pat in paths:
        for f in glob.glob(pat):
            d = json.load(open(f, encoding="utf-8"))
            recs = d.get("records", [])
            pc = per_conv(recs)
            req_e2e = [r["e2e_ms"] for r in recs if r.get("ok") is not False]
            rows.append({
                "file": f.split("\\")[-1].split("/")[-1],
                "sessions": d.get("sessions"),
                "makespan_s": d.get("summary", {}).get("makespan"),
                "per_conv_e2e_s": {k: round(v / 1000.0, 2) for k, v in pc.items()},
                "gini_conv": round(gini(pc.values()) or 0.0, 4),
                "gini_req": round(gini(req_e2e) or 0.0, 4),
                "max_over_min_conv": (round(max(pc.values()) / min(pc.values()), 2)
                                      if pc and min(pc.values()) > 0 else None),
            })
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:] or ["*.json"])
