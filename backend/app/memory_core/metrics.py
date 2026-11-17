"""
Metrics Collection Service (M7)

Metrics collection for ingestion, DLP, retrieval, and deletion operations.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class MetricType(str, Enum):
    """Metric types"""
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class Metric:
    """Individual metric"""
    name: str
    metric_type: MetricType
    value: float
    labels: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MetricSeries:
    """Time series of metrics"""
    name: str
    metric_type: MetricType
    values: List[Metric] = field(default_factory=list)

    def add_value(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Add value to series"""
        metric = Metric(
            name=self.name,
            metric_type=self.metric_type,
            value=value,
            labels=labels or {},
        )
        self.values.append(metric)


class MetricsCollector:
    """
    Metrics collector for memory system operations.

    Features:
    - Counter metrics (ingestion count, deletion count)
    - Gauge metrics (active items, cache size)
    - Histogram metrics (latency, token usage)
    - Label-based filtering
    """

    def __init__(self):
        self.counters: Dict[str, MetricSeries] = {}
        self.gauges: Dict[str, MetricSeries] = {}
        self.histograms: Dict[str, MetricSeries] = {}

    def increment_counter(
        self,
        name: str,
        value: float = 1.0,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Increment counter metric"""
        if name not in self.counters:
            self.counters[name] = MetricSeries(
                name=name,
                metric_type=MetricType.COUNTER,
            )

        self.counters[name].add_value(value, labels)
        logger.debug(f"Incremented counter {name}: {value}")

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Set gauge metric"""
        if name not in self.gauges:
            self.gauges[name] = MetricSeries(
                name=name,
                metric_type=MetricType.GAUGE,
            )

        self.gauges[name].add_value(value, labels)
        logger.debug(f"Set gauge {name}: {value}")

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """Observe histogram metric"""
        if name not in self.histograms:
            self.histograms[name] = MetricSeries(
                name=name,
                metric_type=MetricType.HISTOGRAM,
            )

        self.histograms[name].add_value(value, labels)
        logger.debug(f"Observed histogram {name}: {value}")

    def get_counter(self, name: str) -> float:
        """Get counter value"""
        if name not in self.counters:
            return 0.0

        series = self.counters[name]
        return sum(m.value for m in series.values)

    def get_gauge(self, name: str) -> float:
        """Get gauge value"""
        if name not in self.gauges:
            return 0.0

        series = self.gauges[name]
        if not series.values:
            return 0.0

        return series.values[-1].value

    def get_histogram_stats(self, name: str) -> Dict[str, float]:
        """Get histogram statistics"""
        if name not in self.histograms:
            return {
                "count": 0,
                "sum": 0.0,
                "avg": 0.0,
                "min": 0.0,
                "max": 0.0,
            }

        series = self.histograms[name]
        values = [m.value for m in series.values]

        if not values:
            return {
                "count": 0,
                "sum": 0.0,
                "avg": 0.0,
                "min": 0.0,
                "max": 0.0,
            }

        return {
            "count": len(values),
            "sum": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics"""
        return {
            "counters": {
                name: self.get_counter(name)
                for name in self.counters
            },
            "gauges": {
                name: self.get_gauge(name)
                for name in self.gauges
            },
            "histograms": {
                name: self.get_histogram_stats(name)
                for name in self.histograms
            },
        }

    def reset(self) -> None:
        """Reset all metrics"""
        self.counters.clear()
        self.gauges.clear()
        self.histograms.clear()
        logger.info("Reset all metrics")


class MemoryMetrics:
    """
    Predefined metrics for memory system.

    Metrics:
    - ingestion_total: Total ingestion operations
    - ingestion_dlp_blocked: DLP blocked ingestions
    - items_active: Active memory items
    - items_expired: Expired memory items
    - items_deleted: Deleted memory items
    - retrieval_total: Total retrieval operations
    - retrieval_latency_ms: Retrieval latency
    - retrieval_token_usage: Token usage
    - cache_hits: Cache hits
    - cache_misses: Cache misses
    """

    def __init__(self, collector: MetricsCollector):
        self.collector = collector

    def record_ingestion(self, tenant_id: str, blocked: bool = False) -> None:
        """Record ingestion operation"""
        self.collector.increment_counter(
            "ingestion_total",
            labels={"tenant_id": tenant_id},
        )

        if blocked:
            self.collector.increment_counter(
                "ingestion_dlp_blocked",
                labels={"tenant_id": tenant_id},
            )

    def record_item_status(self, status: str, count: int) -> None:
        """Record item status count"""
        self.collector.set_gauge(
            f"items_{status}",
            count,
        )

    def record_retrieval(
        self,
        latency_ms: float,
        token_usage: int,
        cache_hit: bool,
    ) -> None:
        """Record retrieval operation"""
        self.collector.increment_counter("retrieval_total")

        self.collector.observe_histogram(
            "retrieval_latency_ms",
            latency_ms,
        )

        self.collector.observe_histogram(
            "retrieval_token_usage",
            token_usage,
        )

        if cache_hit:
            self.collector.increment_counter("cache_hits")
        else:
            self.collector.increment_counter("cache_misses")

    def record_deletion(self, tenant_id: str) -> None:
        """Record deletion operation"""
        self.collector.increment_counter(
            "deletion_total",
            labels={"tenant_id": tenant_id},
        )

    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary"""
        return self.collector.get_all_metrics()
