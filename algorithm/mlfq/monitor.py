"""GPU / vLLM 指标采样器(尽力而为, 失败不影响主流程)。

周期性读取:
    - vLLM /metrics: num_requests_running / num_requests_waiting / gpu_cache_usage_perc
    - mx-smi: GPU 硬件利用率
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from typing import Any

_METRIC_RE = re.compile(r"^([a-zA-Z0-9_:]+)(\{[^}]*\})?\s+([0-9.eE+-]+|NaN)")


def _parse_prom(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if m:
            try:
                out[m.group(1)] = float(m.group(3))
            except ValueError:
                continue
    return out


def _sample_mx_smi() -> float | None:
    try:
        proc = subprocess.run(
            ["mx-smi"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    text = proc.stdout or ""
    # GPU-Util 列形如 "| 0%          Disabled |", 匹配 "|" 后的百分比
    utils = re.findall(r"\|\s*(\d+)%", text)
    nums = [int(u) for u in utils]
    if not nums:
        return None
    return float(sum(nums)) / len(nums)


class GPUMonitor:
    def __init__(self, base_url: str, interval_seconds: float = 1.0, use_mx_smi: bool = True):
        self.base_url = base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.use_mx_smi = use_mx_smi
        self.samples: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            await self._task

    async def _fetch_metrics(self) -> dict[str, float]:
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)
            ) as session:
                async with session.get(f"{self.base_url}/metrics") as resp:
                    if resp.status != 200:
                        return {}
                    return _parse_prom(await resp.text())
        except Exception:
            return {}

    async def _loop(self) -> None:
        while not self._stop:
            sample: dict[str, Any] = {"t": round(time.monotonic(), 3)}
            m = await self._fetch_metrics()
            sample["num_requests_running"] = m.get("vllm:num_requests_running")
            sample["num_requests_waiting"] = m.get("vllm:num_requests_waiting")
            sample["kv_cache_usage_perc"] = m.get("vllm:kv_cache_usage_perc")
            sample["prefix_cache_queries_total"] = m.get("vllm:prefix_cache_queries_total")
            sample["prefix_cache_hits_total"] = m.get("vllm:prefix_cache_hits_total")
            if self.use_mx_smi:
                sample["gpu_util"] = _sample_mx_smi()
            self.samples.append(sample)
            await asyncio.sleep(self.interval_seconds)

    def _prefix_hit_rate(self) -> float | None:
        q = [
            s["prefix_cache_queries_total"]
            for s in self.samples
            if isinstance(s.get("prefix_cache_queries_total"), (int, float))
        ]
        h = [
            s["prefix_cache_hits_total"]
            for s in self.samples
            if isinstance(s.get("prefix_cache_hits_total"), (int, float))
        ]
        if len(q) >= 2 and len(h) >= 2:
            dq = q[-1] - q[0]
            dh = h[-1] - h[0]
            if dq > 0:
                return round(dh / dq, 4)
        return None

    def summary(self) -> dict[str, Any]:
        def agg(key: str):
            vals = [s[key] for s in self.samples if isinstance(s.get(key), (int, float))]
            if not vals:
                return {"mean": None, "max": None, "count": 0}
            return {
                "mean": round(sum(vals) / len(vals), 3),
                "max": round(max(vals), 3),
                "count": len(vals),
            }

        return {
            "num_requests_running": agg("num_requests_running"),
            "num_requests_waiting": agg("num_requests_waiting"),
            "kv_cache_usage_perc": agg("kv_cache_usage_perc"),
            "gpu_util": agg("gpu_util"),
            "prefix_cache_hit_rate": self._prefix_hit_rate(),
        }
