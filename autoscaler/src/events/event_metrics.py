#!/usr/bin/env python3
"""
Event Metrics for Prometheus
Track event bus performance and activity
"""

import logging
from typing import Optional
from prometheus_client import Counter, Histogram, Gauge

logger = logging.getLogger(__name__)

# Event Bus Metrics
EVENT_BUS_PUBLISHED_TOTAL = Counter(
    'autoscaler_event_bus_published_total',
    'Total events published to event bus',
    ['event_type', 'status']
)

EVENT_BUS_PROCESSED_TOTAL = Counter(
    'autoscaler_event_bus_processed_total',
    'Total events processed by handlers',
    ['event_type', 'handler', 'status']
)

EVENT_BUS_QUEUE_SIZE = Gauge(
    'autoscaler_event_bus_queue_size',
    'Current number of events in the queue',
    ['queue_type']
)

EVENT_BUS_SUBSCRIBERS = Gauge(
    'autoscaler_event_bus_subscribers_count',
    'Number of subscribers per event type',
    ['event_type']
)

EVENT_BUS_HANDLER_DURATION = Histogram(
    'autoscaler_event_bus_handler_duration_seconds',
    'Time taken to process events by handlers',
    ['event_type', 'handler'],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

EVENT_BUS_ERRORS_TOTAL = Counter(
    'autoscaler_event_bus_errors_total',
    'Total event bus errors',
    ['error_type', 'component']
)

EVENT_BUS_HEALTH = Gauge(
    'autoscaler_event_bus_health',
    'Event bus health status (1=healthy, 0=unhealthy)',
    ['component']
)

EVENT_BUS_EVENTS_RATE = Gauge(
    'autoscaler_event_bus_events_per_second',
    'Events per second (rolling average)',
    ['direction']  # 'published' or 'processed'
)

# Event Type Specific Metrics
OPTIMAL_STATE_EVENTS = Counter(
    'autoscaler_optimal_state_events_total',
    'Optimal state trigger events',
    ['trigger_reason']
)

SCALING_EVENTS = Counter(
    'autoscaler_scaling_events_total',
    'Scaling action events',
    ['action', 'status']
)

CLUSTER_STATE_EVENTS = Counter(
    'autoscaler_cluster_state_events_total',
    'Cluster state sync events',
    ['sync_type', 'status']
)

RESOURCE_PRESSURE_EVENTS = Counter(
    'autoscaler_resource_pressure_events_total',
    'Resource pressure alerts',
    ['resource_type', 'pressure_level']
)

class EventMetricsCollector:
    """Collects and manages event metrics"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # Track event rates
        self._published_count = 0
        self._processed_count = 0
        self._last_publish_time = None
        self._last_process_time = None

    def record_event_published(self, event_type: str, success: bool):
        """Record an event publication attempt"""
        status = 'success' if success else 'failed'
        EVENT_BUS_PUBLISHED_TOTAL.labels(event_type=event_type, status=status).inc()

        if success:
            self._published_count += 1
            self._last_publish_time = self._get_current_time()
            self._update_event_rate('published')

    def record_event_processed(self, event_type: str, handler: str, success: bool, duration: float):
        """Record an event processing attempt"""
        status = 'success' if success else 'failed'

        # Update counters and histogram
        EVENT_BUS_PROCESSED_TOTAL.labels(
            event_type=event_type,
            handler=handler,
            status=status
        ).inc()

        EVENT_BUS_HANDLER_DURATION.labels(
            event_type=event_type,
            handler=handler
        ).observe(duration)

        if success:
            self._processed_count += 1
            self._last_process_time = self._get_current_time()
            self._update_event_rate('processed')

    def update_queue_size(self, queue_type: str, size: int):
        """Update queue size metric"""
        EVENT_BUS_QUEUE_SIZE.labels(queue_type=queue_type).set(size)

    def update_subscriber_count(self, event_type: str, count: int):
        """Update subscriber count metric"""
        EVENT_BUS_SUBSCRIBERS.labels(event_type=event_type).set(count)

    def record_error(self, error_type: str, component: str):
        """Record an error in the event system"""
        EVENT_BUS_ERRORS_TOTAL.labels(error_type=error_type, component=component).inc()

    def update_health(self, component: str, healthy: bool):
        """Update component health status"""
        health_value = 1 if healthy else 0
        EVENT_BUS_HEALTH.labels(component=component).set(health_value)

    def record_optimal_state_event(self, trigger_reason: str):
        """Record optimal state trigger event"""
        OPTIMAL_STATE_EVENTS.labels(trigger_reason=trigger_reason).inc()

    def record_scaling_event(self, action: str, status: str):
        """Record scaling event"""
        SCALING_EVENTS.labels(action=action, status=status).inc()

    def record_cluster_state_event(self, sync_type: str, status: str):
        """Record cluster state sync event"""
        CLUSTER_STATE_EVENTS.labels(sync_type=sync_type, status=status).inc()

    def record_resource_pressure(self, resource_type: str, pressure_level: str):
        """Record resource pressure event"""
        RESOURCE_PRESSURE_EVENTS.labels(
            resource_type=resource_type,
            pressure_level=pressure_level
        ).inc()

    def _get_current_time(self) -> float:
        """Get current timestamp in seconds"""
        import time
        return time.time()

    def _update_event_rate(self, direction: str):
        """Update events per second gauge"""
        # Simple implementation - could be improved with sliding window
        if direction == 'published' and self._last_publish_time:
            # Rough calculation - could be more sophisticated
            rate = self._published_count / max(1, self._get_current_time() - self._last_publish_time + 1)
            EVENT_BUS_EVENTS_RATE.labels(direction='published').set(rate)
        elif direction == 'processed' and self._last_process_time:
            rate = self._processed_count / max(1, self._get_current_time() - self._last_process_time + 1)
            EVENT_BUS_EVENTS_RATE.labels(direction='processed').set(rate)


# Global metrics collector instance
metrics_collector = EventMetricsCollector()