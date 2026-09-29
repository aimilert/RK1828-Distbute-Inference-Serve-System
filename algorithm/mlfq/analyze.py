"""结果分析: 量化五个调度因子对吞吐/延迟/Bubble 的影响权重。

方法:
    1. 消融(单因子 vs FCFS 基线): 相对变化量归一化为影响权重。
    2. 多元线性回归(标准化系数): 各因子权重对指标的边际影响(标准差单位)。
输出: factor_weights.json / factor_weights.md
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from config import FACTOR_LABELS, FACTOR_NAMES, POLICIES, RESULTS_DIR

METRICS = {
    "bubble_ratio": "bubble_ratio",
    "e2e_p50": "e2e_p50",
    "e2e_p99": "e2e_p99",
    "ttft_p99": "ttft_p99",
    "makespan": "makespan",
    "output_tokens_s": "output_tokens_s",
}
DIRECTION = {
    "bubble_ratio": -1,
    "e2e_p50": -1,
    "e2e_p99": -1,
    "ttft_p99": -1,
    "makespan": -1,
    "output_tokens_s": 1,
}
METRIC_LABELS = {
    "bubble_ratio": "Bubble (气泡比)",
    "e2e_p50": "E2E P50 (s)",
    "e2e_p99": "E2E P99 (s)",
    "ttft_p99": "TTFT P99 (s)",
    "makespan": "Makespan (s)",
    "output_tokens_s": "吞吐 (tok/s)",
}


def load_results(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for k in list(row.keys()):
                if k == "weights":
                    row[k] = json.loads(row[k])
                elif k == "policy":
                    pass
                else:
                    try:
                        row[k] = float(row[k])
                    except (TypeError, ValueError):
                        pass
            rows.append(row)
    return rows


def _zscore(x: np.ndarray) -> np.ndarray:
    std = x.std()
    if std == 0:
        return np.zeros_like(x)
    return (x - x.mean()) / std


def ablation_weights(rows: list[dict]) -> dict:
    by_policy = {r["policy"]: r for r in rows}
    base = by_policy.get("fcfs")
    if base is None:
        raise RuntimeError("缺少 fcfs 基线结果")
    out: dict[str, dict] = {}
    for metric, field in METRICS.items():
        direction = DIRECTION[metric]
        base_v = float(base[field])
        impacts: dict[str, float] = {}
        signed: dict[str, float] = {}
        for f in FACTOR_NAMES:
            p = by_policy.get(f)
            if p is None:
                continue
            v = float(p[field])
            delta = v - base_v
            improvement = direction * delta
            rel = improvement / abs(base_v) if base_v != 0 else improvement
            impacts[f] = abs(rel)
            signed[f] = rel
        total = sum(impacts.values())
        weights = {f: (impacts[f] / total if total > 0 else 0.0) for f in FACTOR_NAMES}
        out[metric] = {"base": base_v, "weights": weights, "signed_relative_impact": signed}
    return out


def regression_coefficients(rows: list[dict]) -> dict:
    use = [r for r in rows if r["policy"] in POLICIES]
    X = np.array([[float(w) for w in r["weights"]] for r in use])
    out: dict[str, dict] = {}
    for metric, field in METRICS.items():
        y = np.array([float(r[field]) for r in use])
        Xs = np.column_stack(
            [np.ones(len(use))] + [_zscore(X[:, j]) for j in range(len(FACTOR_NAMES))]
        )
        ys = _zscore(y)
        coef, *_ = np.linalg.lstsq(Xs, ys, rcond=None)
        out[metric] = {FACTOR_NAMES[j]: float(coef[1 + j]) for j in range(len(FACTOR_NAMES))}
    return out


def main() -> None:
    csv_path = RESULTS_DIR / "results.csv"
    if not csv_path.exists():
        print(f"找不到 {csv_path}, 请先运行 runner.py")
        return
    rows = load_results(csv_path)

    ablation = ablation_weights(rows)
    reg = regression_coefficients(rows)

    payload = {
        "policies": [r["policy"] for r in rows],
        "ablation_weights": ablation,
        "regression_std_coef": reg,
        "direction": DIRECTION,
    }
    out_json = RESULTS_DIR / "factor_weights.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 上层封装调度因子影响权重分析", ""]
    lines.append("基于 TraceLab 真实 agent workload(多会话串行链 + 工具延迟),在 Qwen3-8B (vLLM-metax, C500) 上实测。")
    lines.append("")
    lines.append("## 1. 策略结果总览")
    lines.append("")
    lines.append("| 策略 | Makespan(s) | E2E P50(s) | E2E P99(s) | TTFT P99(s) | 吞吐(tok/s) | Bubble 比 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['policy']} | {r['makespan']:.2f} | {r['e2e_p50']:.3f} | {r['e2e_p99']:.3f} "
            f"| {r['ttft_p99']:.3f} | {r['output_tokens_s']:.1f} | {r['bubble_ratio']:.4f} |"
        )
    lines.append("")
    lines.append("## 2. 消融法影响权重(单因子 vs FCFS, 归一化)")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(FACTOR_LABELS.values()) + " |")
    lines.append("|---|" + "---|" * len(FACTOR_NAMES))
    for metric, info in ablation.items():
        w = info["weights"]
        lines.append(
            f"| {METRIC_LABELS[metric]} | "
            + " | ".join(f"{w[f]*100:.1f}%" for f in FACTOR_NAMES)
            + " |"
        )
    lines.append("")
    lines.append("## 3. 回归标准化系数(边际影响, 正值=提高该权重使指标上升)")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(FACTOR_LABELS.values()) + " |")
    lines.append("|---|" + "---|" * len(FACTOR_NAMES))
    for metric, c in reg.items():
        lines.append(
            f"| {METRIC_LABELS[metric]} | "
            + " | ".join(f"{c[f]:+.3f}" for f in FACTOR_NAMES)
            + " |"
        )
    lines.append("")
    lines.append("注: 消融权重表示各因子单独施加时对指标的相对影响占比;")
    lines.append("回归系数表示在混合策略中,提高某因子权重对该指标的边际效应(标准化)。")
    out_md = RESULTS_DIR / "factor_weights.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"权重 JSON : {out_json}")
    print(f"报告 MD   : {out_md}")


if __name__ == "__main__":
    main()
