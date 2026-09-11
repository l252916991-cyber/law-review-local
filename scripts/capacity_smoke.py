"""E33 capacity smoke harness: bounded concurrent reads plus bounded model requests.

This is a measurement tool, not a benchmark claim. It reports client-observed latency,
error rate, resource samples and recovery time for the protocol in
``docs/runbook/deployment-acceptance.md`` section 6 (<=50 concurrent reads, <=5 model
requests). Results define an observed range only; they are never an SLA.

Usage::

    python scripts/capacity_seed.py --data-dir /tmp/lexvault-capacity --pages-per-doc 50
    python scripts/capacity_smoke.py --base-url http://127.0.0.1:8791 --case-id 1 \
        --readers 50 --read-seconds 60 --model-concurrency 5 --model-requests 15 \
        --server-pid 12345 --db-path /tmp/lexvault-capacity/law_review.db \
        --out output/capacity/read-1000.json --markdown-out output/capacity/read-1000.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # httpx is a dev-group dependency; import lazily at runtime.
    import httpx

SEARCH_QUERY = "借款合同转账人民币"
MODEL_QUESTION = "请根据卷宗材料说明本案资金往来的主要事实，并标注来源。"


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class Result:
    phase: str
    endpoint: str
    status: int
    latency_ms: float
    error: str = ""


@dataclass
class Counter:
    """Thread-safe in-flight counter used as an observed queue proxy."""

    value: int = 0
    peak: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def enter(self) -> None:
        with self._lock:
            self.value += 1
            self.peak = max(self.peak, self.value)

    def leave(self) -> None:
        with self._lock:
            self.value = max(0, self.value - 1)

    def snapshot(self) -> int:
        with self._lock:
            return self.value


def percentile(values: Sequence[float], percent: float) -> float:
    """Nearest-rank percentile: no interpolation, so the value is an observed sample."""
    if not values:
        return 0.0
    if not 0 < percent <= 100:
        raise ValueError("percent must be in (0, 100]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return float(ordered[rank - 1])


def summarize(results: Sequence[Result]) -> dict[str, Any]:
    latencies = [item.latency_ms for item in results]
    errors = [item for item in results if item.status < 200 or item.status >= 400]
    status_counts: dict[str, int] = {}
    for item in results:
        key = str(item.status) if not item.error else f"client_error:{item.error}"
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "requests": len(results),
        "errors": len(errors),
        "error_rate": round(len(errors) / len(results), 4) if results else 0.0,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "min_ms": round(min(latencies), 1) if latencies else 0.0,
        "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "status_counts": dict(sorted(status_counts.items())),
    }


def summarize_by_endpoint(results: Sequence[Result]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Result]] = {}
    for item in results:
        grouped.setdefault(item.endpoint, []).append(item)
    return {name: summarize(items) for name, items in sorted(grouped.items())}


def build_endpoints(case_id: int, document_id: int | None, page_no: int) -> list[Endpoint]:
    endpoints = [
        Endpoint("cases-list", "GET", "/api/cases"),
        Endpoint("case-detail", "GET", f"/api/cases/{case_id}"),
        Endpoint("documents", "GET", f"/api/cases/{case_id}/documents"),
        Endpoint("evidence", "GET", f"/api/cases/{case_id}/evidence"),
        Endpoint("search", "GET", f"/api/cases/{case_id}/search?q={SEARCH_QUERY}"),
        Endpoint("audit", "GET", f"/api/cases/{case_id}/audit"),
        Endpoint("dashboard", "GET", "/api/dashboard"),
        Endpoint("platform-metrics", "GET", f"/api/cases/{case_id}/platform-metrics"),
    ]
    if document_id:
        endpoints.append(Endpoint("page-text", "GET", f"/api/documents/{document_id}/pages/{page_no}"))
    return endpoints


def build_model_endpoint(case_id: int) -> Endpoint:
    return Endpoint("chat-model", "POST", f"/api/cases/{case_id}/chat", {"question": MODEL_QUESTION, "use_llm": True})


def _new_client(timeout: float, base_url: str = "") -> "httpx.Client":
    import httpx

    return httpx.Client(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
    )


def request_once(client: "httpx.Client", endpoint: Endpoint, headers: dict[str, str], phase: str) -> Result:
    started = time.perf_counter()
    try:
        if endpoint.method == "POST":
            response = client.post(endpoint.path, json=endpoint.body or {}, headers=headers)
        else:
            response = client.get(endpoint.path, headers=headers)
        latency = (time.perf_counter() - started) * 1000
        return Result(phase, endpoint.name, response.status_code, latency)
    except Exception as exc:  # network/timeout failures are data, not crashes
        latency = (time.perf_counter() - started) * 1000
        return Result(phase, endpoint.name, 0, latency, type(exc).__name__)


def baseline_phase(
    endpoints: Sequence[Endpoint], headers: dict[str, str], samples: int, timeout: float, base_url: str = ""
) -> tuple[list[Result], dict[str, Any]]:
    results: list[Result] = []
    client = _new_client(timeout, base_url)
    try:
        for index in range(samples):
            endpoint = endpoints[index % len(endpoints)]
            results.append(request_once(client, endpoint, headers, "baseline"))
    finally:
        client.close()
    return results, summarize(results)


def read_phase(
    endpoints: Sequence[Endpoint],
    headers: dict[str, str],
    readers: int,
    seconds: float,
    timeout: float,
    counter: Counter,
    base_url: str = "",
) -> list[Result]:
    results: list[Result] = []
    lock = threading.Lock()
    stop_at = time.monotonic() + seconds

    def worker(worker_index: int) -> None:
        client = _new_client(timeout, base_url)
        local: list[Result] = []
        iteration = 0
        try:
            while time.monotonic() < stop_at:
                endpoint = endpoints[(worker_index + iteration) % len(endpoints)]
                counter.enter()
                try:
                    local.append(request_once(client, endpoint, headers, "read-load"))
                finally:
                    counter.leave()
                iteration += 1
        finally:
            client.close()
            with lock:
                results.extend(local)

    threads = [threading.Thread(target=worker, args=(index,), name=f"reader-{index}") for index in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def model_phase(
    endpoint: Endpoint,
    headers: dict[str, str],
    concurrency: int,
    requests: int,
    timeout: float,
    counter: Counter,
    base_url: str = "",
) -> list[Result]:
    results: list[Result] = []
    lock = threading.Lock()
    index_lock = threading.Lock()
    next_index = 0

    def worker() -> None:
        nonlocal next_index
        client = _new_client(timeout, base_url)
        local: list[Result] = []
        try:
            while True:
                with index_lock:
                    if next_index >= requests:
                        return
                    next_index += 1
                counter.enter()
                try:
                    local.append(request_once(client, endpoint, headers, "model-load"))
                finally:
                    counter.leave()
        finally:
            client.close()
            with lock:
                results.extend(local)

    threads = [threading.Thread(target=worker, name=f"model-{i}") for i in range(max(1, concurrency))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def sample_process(pid: int) -> dict[str, float] | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_ps_output(completed.stdout)


def parse_ps_output(text: str) -> dict[str, float] | None:
    """Parse ``ps -o %cpu=,rss=`` output; split out for offline unit tests."""
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        return {"cpu_percent": float(parts[0]), "rss_mb": round(float(parts[1]) / 1024, 1)}
    except ValueError:
        return None


class ResourceSampler:
    """Samples the server process, disk and SQLite WAL without touching the application."""

    def __init__(
        self,
        server_pid: int | None,
        model_pid: int | None,
        db_path: Path | None,
        interval: float = 0.5,
        counter: Counter | None = None,
    ) -> None:
        self.server_pid = server_pid
        self.model_pid = model_pid
        self.db_path = db_path
        self.interval = interval
        self.counter = counter
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _one(self) -> dict[str, Any]:
        sample: dict[str, Any] = {"t": round(time.monotonic(), 3)}
        if self.server_pid:
            server = sample_process(self.server_pid)
            if server:
                sample["server_cpu_percent"] = server["cpu_percent"]
                sample["server_rss_mb"] = server["rss_mb"]
        if self.model_pid:
            model = sample_process(self.model_pid)
            if model:
                sample["model_cpu_percent"] = model["cpu_percent"]
                sample["model_rss_mb"] = model["rss_mb"]
        if self.db_path:
            wal = self.db_path.with_name(self.db_path.name + "-wal")
            sample["wal_mb"] = round(wal.stat().st_size / 1024 / 1024, 2) if wal.exists() else 0.0
            sample["db_mb"] = round(self.db_path.stat().st_size / 1024 / 1024, 2) if self.db_path.exists() else 0.0
            usage = shutil.disk_usage(self.db_path.parent)
            sample["disk_free_gb"] = round(usage.free / 1024 / 1024 / 1024, 2)
        if self.counter is not None:
            sample["inflight"] = self.counter.snapshot()
        return sample

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self._one())
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.samples.append(self._one())
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> list[dict[str, Any]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.samples.append(self._one())
        return self.samples


def peak_summary(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def peak(key: str) -> float:
        values = [float(item[key]) for item in samples if key in item]
        return round(max(values), 2) if values else 0.0

    def minimum(key: str) -> float:
        values = [float(item[key]) for item in samples if key in item]
        return round(min(values), 2) if values else 0.0

    return {
        "samples": len(samples),
        "server_cpu_percent_peak": peak("server_cpu_percent"),
        "server_rss_mb_peak": peak("server_rss_mb"),
        "model_cpu_percent_peak": peak("model_cpu_percent"),
        "model_rss_mb_peak": peak("model_rss_mb"),
        "wal_mb_peak": peak("wal_mb"),
        "db_mb_peak": peak("db_mb"),
        "disk_free_gb_min": minimum("disk_free_gb"),
        "inflight_peak": peak("inflight"),
    }


def recovery_phase(
    endpoints: Sequence[Endpoint],
    headers: dict[str, str],
    threshold_ms: float,
    timeout: float,
    poll: float = 0.25,
    base_url: str = "",
) -> dict[str, Any]:
    """Time until readiness plus five consecutive reads within the baseline p95 budget."""
    client = _new_client(timeout, base_url)
    started = time.monotonic()
    consecutive = 0
    observed: list[float] = []
    try:
        while time.monotonic() - started < timeout:
            health = request_once(client, Endpoint("health", "GET", "/api/system/health"), headers, "recovery")
            probe = request_once(client, endpoints[1], headers, "recovery")
            observed.append(round(probe.latency_ms, 1))
            healthy = health.status == 200 and probe.status == 200 and probe.latency_ms <= threshold_ms
            consecutive = consecutive + 1 if healthy else 0
            if consecutive >= 5:
                return {
                    "recovered": True,
                    "seconds": round(time.monotonic() - started, 2),
                    "threshold_ms": round(threshold_ms, 1),
                    "probe_latencies_ms": observed,
                }
            time.sleep(poll)
    finally:
        client.close()
    return {
        "recovered": False,
        "seconds": round(time.monotonic() - started, 2),
        "threshold_ms": round(threshold_ms, 1),
        "probe_latencies_ms": observed,
    }


def render_markdown(result: dict[str, Any]) -> str:
    environment = result["environment"]
    baseline = result["baseline"]
    load = result["read_load"]
    model = result.get("model_load")
    peaks = result["peaks"]
    recovery = result["recovery"]
    lines = [
        "# 容量冒烟原始结果",
        "",
        "> 本文件由 `scripts/capacity_smoke.py` 生成。数据仅定义本次实测可支持范围，不构成 SLA。",
        "",
        "## 环境",
        "",
        f"- 时间：{environment['started_at']}",
        f"- 平台：{environment['platform']} / Python {environment['python']} / CPU 核 {environment['cpu_count']}",
        f"- 目标：`{environment['base_url']}`，案件 {environment['case_id']}，并发读 {environment['readers']}，读时长 {environment['read_seconds']}s",
        f"- 模型请求并发 {environment['model_concurrency']}，请求数 {environment['model_requests']}",
        "",
        "## 读并发",
        "",
        f"- 基线（串行）：p50 {baseline['p50_ms']}ms，p95 {baseline['p95_ms']}ms，n={baseline['requests']}",
        f"- 负载：请求 {load['requests']}，错误 {load['errors']}（{load['error_rate']:.2%}），"
        f"p50 {load['p50_ms']}ms，p95 {load['p95_ms']}ms，p99 {load['p99_ms']}ms，max {load['max_ms']}ms",
        "",
        "| 端点 | 请求 | 错误率 | p50 | p95 | p99 | max |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, item in result["read_by_endpoint"].items():
        lines.append(
            f"| {name} | {item['requests']} | {item['error_rate']:.2%} | {item['p50_ms']}ms | "
            f"{item['p95_ms']}ms | {item['p99_ms']}ms | {item['max_ms']}ms |"
        )
    lines += ["", "## 模型并发", ""]
    if model:
        lines += [
            f"- 请求 {model['requests']}，错误 {model['errors']}（{model['error_rate']:.2%}），"
            f"p50 {model['p50_ms']}ms，p95 {model['p95_ms']}ms，max {model['max_ms']}ms",
        ]
    else:
        lines.append("- 未执行（`--model-requests 0`）。")
    lines += [
        "",
        "## 资源峰值",
        "",
        f"- 服务进程 CPU {peaks['server_cpu_percent_peak']}%，RSS {peaks['server_rss_mb_peak']}MB",
        f"- 模型进程 CPU {peaks['model_cpu_percent_peak']}%，RSS {peaks['model_rss_mb_peak']}MB",
        f"- WAL {peaks['wal_mb_peak']}MB，数据库 {peaks['db_mb_peak']}MB，磁盘剩余 {peaks['disk_free_gb_min']}GB",
        f"- 在途请求峰值 {peaks['inflight_peak']}（采样 {peaks['samples']} 次）",
        "",
        "## 恢复",
        "",
        f"- 恢复到基线 p95 以内并连续 5 次通过：{'是' if recovery['recovered'] else '否'}，"
        f"耗时 {recovery['seconds']}s（阈值 {recovery['threshold_ms']}ms）",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the E33 capacity smoke protocol against a local LexVault instance.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--token", help="Bearer token when the server runs in token mode")
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--document-id", type=int)
    parser.add_argument("--page-no", type=int, default=1)
    parser.add_argument("--readers", type=int, default=50)
    parser.add_argument("--read-seconds", type=float, default=60.0)
    parser.add_argument("--baseline-samples", type=int, default=24)
    parser.add_argument("--model-concurrency", type=int, default=5)
    parser.add_argument("--model-requests", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--recovery-timeout", type=float, default=60.0)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--model-pid", type=int)
    parser.add_argument("--db-path", help="SQLite file path for WAL/disk sampling")
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--label", default="capacity-smoke")
    parser.add_argument("--out", help="Write the full JSON result here")
    parser.add_argument("--markdown-out", help="Write the rendered Markdown result here")
    arguments = parser.parse_args()

    headers = {"Authorization": f"Bearer {arguments.token}"} if arguments.token else {}
    endpoints = build_endpoints(arguments.case_id, arguments.document_id, arguments.page_no)
    db_path = Path(arguments.db_path) if arguments.db_path else None

    read_counter = Counter()
    read_sampler = ResourceSampler(arguments.server_pid, arguments.model_pid, db_path, arguments.sample_interval, read_counter)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S%z")

    baseline_results, baseline = baseline_phase(endpoints, headers, arguments.baseline_samples, arguments.timeout, arguments.base_url)

    read_sampler.start()
    read_results = read_phase(endpoints, headers, arguments.readers, arguments.read_seconds, arguments.timeout, read_counter, arguments.base_url)
    read_samples = read_sampler.stop()
    load = summarize(read_results)

    model_summary: dict[str, Any] | None = None
    model_samples: list[dict[str, Any]] = []
    if arguments.model_requests > 0:
        model_counter = Counter()
        model_sampler = ResourceSampler(
            arguments.server_pid, arguments.model_pid, db_path, arguments.sample_interval, model_counter
        )
        model_sampler.start()
        model_results = model_phase(
            build_model_endpoint(arguments.case_id),
            headers,
            arguments.model_concurrency,
            arguments.model_requests,
            arguments.timeout,
            model_counter,
            arguments.base_url,
        )
        model_samples = model_sampler.stop()
        model_summary = summarize(model_results)

    threshold = max(baseline["p95_ms"], 1.0)
    recovery = recovery_phase(endpoints, headers, threshold, arguments.recovery_timeout, base_url=arguments.base_url)

    result: dict[str, Any] = {
        "label": arguments.label,
        "environment": {
            "started_at": started_at,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "base_url": arguments.base_url,
            "case_id": arguments.case_id,
            "readers": arguments.readers,
            "read_seconds": arguments.read_seconds,
            "model_concurrency": arguments.model_concurrency,
            "model_requests": arguments.model_requests,
            "server_pid": arguments.server_pid,
            "model_pid": arguments.model_pid,
        },
        "baseline": baseline,
        "read_load": load,
        "read_by_endpoint": summarize_by_endpoint(read_results),
        "model_load": model_summary,
        "peaks": peak_summary(read_samples + model_samples),
        "read_samples": read_samples,
        "model_samples": model_samples,
        "recovery": recovery,
        "protocol": {
            "concurrent_reads": arguments.readers,
            "model_requests": arguments.model_requests,
            "note": "Observed range only; not an SLA. Client-side latency includes connection reuse.",
        },
    }

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.out:
        Path(arguments.out).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.out).write_text(rendered, encoding="utf-8")
    markdown = render_markdown(result)
    if arguments.markdown_out:
        Path(arguments.markdown_out).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments.markdown_out).write_text(markdown, encoding="utf-8")
    print(markdown)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
