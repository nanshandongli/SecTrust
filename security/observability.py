"""
可观测性（Observability）—— 零依赖默认，可选接入 OpenTelemetry。

设计取舍：
- 默认（未装 opentelemetry）提供「内存指标收集器 + 结构化 JSON 日志 + 阈值告警」，
  足以本地/小规模运行看清延迟、错误率、吞吐；不引入重依赖。
- 若环境装了 opentelemetry-sdk / opentelemetry-exporter-otlp，可一键启用分布式追踪与
  OTLP 导出（见 enable_otel），与既有的「零硬依赖」原则不冲突（OTel 属可选 extras）。

指标：
- counter：累计计数（如 http_requests_total）
- histogram：观测值分布（如 http_request_duration_seconds）

告警：
- 基于快照的阈值规则（error_rate、p95 延迟、队列积压），evaluate_alerts() 返回触发的告警。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


# ---------------- 指标收集器（零依赖） ----------------
class MetricsCollector:
    """进程内指标收集器：counter / histogram。线程不安全但本服务多为单线程事件循环，
    如需并发可在调用处加锁；这里保持轻量。"""

    def __init__(self):
        self._counters: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._lock = None  # 预留：高并发场景可注入锁

    def inc(self, name: str, amount: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        key = self._key(name, tags)
        self._counters[key] += amount

    def observe(self, name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
        key = self._key(name, tags)
        lst = self._histograms[key]
        lst.append(value)
        # 控制内存：仅保留最近 1000 个样本做分位数估计
        if len(lst) > 1000:
            self._histograms[key] = lst[-1000:]

    @staticmethod
    def _key(name: str, tags: Optional[Dict[str, str]]) -> str:
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}{{{tag_str}}}"

    def snapshot(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"counters": dict(self._counters)}
        hist: Dict[str, Any] = {}
        for name, vals in self._histograms.items():
            if not vals:
                continue
            s = sorted(vals)
            n = len(s)
            hist[name] = {
                "count": n,
                "min": s[0],
                "max": s[-1],
                "mean": sum(s) / n,
                "p50": s[int(n * 0.5)],
                "p95": s[min(n - 1, int(n * 0.95))],
                "p99": s[min(n - 1, int(n * 0.99))],
            }
        out["histograms"] = hist
        return out


_COLLECTOR = MetricsCollector()


def get_metrics() -> MetricsCollector:
    return _COLLECTOR


# ---------------- 结构化日志 ----------------
def log_event(level: str, message: str, **fields: Any) -> None:
    """输出一行 JSON 日志到 stdout（便于采集到 Loki/ELK）。"""
    rec = {
        "ts": time.time(),
        "level": level,
        "msg": message,
    }
    for k, v in fields.items():
        rec[k] = v
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        line = f"{level} {message} {fields}"
    print(line, file=sys.stdout, flush=True)


# ---------------- 追踪（可选 OTel） ----------------
_OTEL_TRACER = None
_OTEL_ENABLED = False


def enable_otel(service_name: str = "ai-colleague", otlp_endpoint: Optional[str] = None) -> bool:
    """尝试初始化 OpenTelemetry（OTLP gRPC 导出）。失败则退回无追踪。返回是否启用成功。"""
    global _OTEL_TRACER, _OTEL_ENABLED
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint or "http://localhost:4317")
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            _OTEL_TRACER = trace.get_tracer(service_name)
            _OTEL_ENABLED = True
            log_event("info", "OpenTelemetry 已启用", service=service_name,
                      endpoint=otlp_endpoint or "http://localhost:4317")
            return True
        except Exception as e:  # noqa: BLE001
            log_event("warn", "OTel OTLP 导出不可用，退回无追踪", error=str(e))
            _OTEL_TRACER = trace.get_tracer(service_name)
            _OTEL_ENABLED = True
            return True
    except ImportError:
        log_event("info", "未安装 opentelemetry，使用无追踪模式（指标/日志仍可用）")
        _OTEL_ENABLED = False
        return False


@contextmanager
def span(name: str, **attrs: Any):
    """跨 OTel/无追踪的统一 span 上下文。"""
    if _OTEL_TRACER is not None:
        with _OTEL_TRACER.start_as_current_span(name) as sp:
            for k, v in attrs.items():
                sp.set_attribute(k, str(v))
            yield sp
    else:
        t0 = time.time()
        try:
            yield None
        finally:
            get_metrics().observe("span_duration_seconds", time.time() - t0, {"span": name})


# ---------------- 告警规则 ----------------
class AlertRule:
    def __init__(self, name: str, metric_key: str, op: str, threshold: float,
                 severity: str = "warning", message: str = ""):
        self.name = name
        self.metric_key = metric_key
        self.op = op  # ">" / "<" / ">="
        self.threshold = threshold
        self.severity = severity
        self.message = message or f"{name} 触发（{metric_key} {op} {threshold}）"

    def check(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        val = self._resolve(snap)
        if val is None:
            return None
        hit = (
            (self.op == ">" and val > self.threshold)
            or (self.op == ">=" and val >= self.threshold)
            or (self.op == "<" and val < self.threshold)
        )
        if hit:
            return {"rule": self.name, "severity": self.severity,
                    "metric": self.metric_key, "value": val, "message": self.message}
        return None

    def _resolve(self, snap: Dict[str, Any]):
        # 支持直方图 p95 的简写：metric_key 形如 histograms.<key>.p95
        if self.metric_key.startswith("histograms."):
            _, hkey, stat = self.metric_key.split(".", 2)
            return snap.get("histograms", {}).get(hkey, {}).get(stat)
        return snap.get("counters", {}).get(self.metric_key)


DEFAULT_ALERT_RULES = [
    AlertRule("error_rate_high", "http_requests_total{status=5xx}", ">", 0.05,
              severity="critical", message="5xx 错误率超过 5%"),
    AlertRule("p95_latency_high", "histograms.http_request_duration_seconds.p95", ">",
              1.0, severity="warning", message="P95 延迟超过 1s"),
]


def evaluate_alerts(snap: Dict[str, Any], rules: Optional[List[AlertRule]] = None) -> List[Dict[str, Any]]:
    rules = rules or DEFAULT_ALERT_RULES
    fired = []
    for r in rules:
        a = r.check(snap)
        if a:
            fired.append(a)
            log_event("alert", a["message"], severity=a["severity"], rule=a["rule"],
                      metric=a["metric"], value=a["value"])
    return fired


def error_rate_from_counters(snap: Dict[str, Any]) -> float:
    """从带 status 标签的计数器推导 5xx 错误率（供告警规则使用）。"""
    total = sum(v for k, v in snap.get("counters", {}).items() if k.startswith("http_requests_total{"))
    err = sum(v for k, v in snap.get("counters", {}).items()
              if k.startswith("http_requests_total{") and "status=5xx" in k)
    return (err / total) if total else 0.0


if __name__ == "__main__":
    m = get_metrics()
    m.inc("http_requests_total", tags={"status": "2xx"})
    m.inc("http_requests_total", tags={"status": "5xx"})
    m.observe("http_request_duration_seconds", 0.12, {"method": "GET"})
    m.observe("http_request_duration_seconds", 1.5, {"method": "POST"})
    snap = m.snapshot()
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    print("alerts:", evaluate_alerts(snap))
