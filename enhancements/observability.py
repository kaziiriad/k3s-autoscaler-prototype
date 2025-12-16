#!/usr/bin/env python3
"""
Enhanced Observability Framework
Includes distributed tracing, structured logging, and comprehensive metrics
"""

import logging
import json
import time
from datetime import datetime
from typing import Dict, Any, Optional
from functools import wraps
from contextvars import ContextVar
import uuid

# Context variable for request tracing
trace_id_var: ContextVar[str] = ContextVar('trace_id', default='')


class StructuredLogger:
    """
    Structured logging for better observability
    Outputs JSON logs that can be easily parsed by log aggregators
    """
    
    def __init__(self, name: str, extra_fields: Optional[Dict[str, Any]] = None):
        """
        Initialize structured logger
        
        Args:
            name: Logger name (usually __name__)
            extra_fields: Additional fields to include in every log
        """
        self.logger = logging.getLogger(name)
        self.extra_fields = extra_fields or {}
        self.service_name = "k3s-autoscaler"
    
    def _format_log(
        self,
        level: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None
    ) -> str:
        """Format log as JSON"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "service": self.service_name,
            "level": level,
            "message": message,
            "trace_id": trace_id_var.get(),
            **self.extra_fields
        }
        
        if extra:
            log_entry.update(extra)
        
        if error:
            log_entry["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "stack_trace": self._get_stack_trace(error)
            }
        
        return json.dumps(log_entry)
    
    def _get_stack_trace(self, error: Exception) -> str:
        """Get formatted stack trace"""
        import traceback
        return ''.join(traceback.format_exception(
            type(error), error, error.__traceback__
        ))
    
    def info(self, message: str, **extra):
        """Log info message"""
        self.logger.info(self._format_log("INFO", message, extra))
    
    def error(self, message: str, error: Optional[Exception] = None, **extra):
        """Log error message"""
        self.logger.error(self._format_log("ERROR", message, extra, error))
    
    def warning(self, message: str, **extra):
        """Log warning message"""
        self.logger.warning(self._format_log("WARNING", message, extra))
    
    def debug(self, message: str, **extra):
        """Log debug message"""
        self.logger.debug(self._format_log("DEBUG", message, extra))


class TracingSpan:
    """
    Distributed tracing span
    Tracks operation timing and metadata
    """
    
    def __init__(
        self,
        operation: str,
        parent_span_id: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize tracing span
        
        Args:
            operation: Name of the operation
            parent_span_id: ID of parent span for nested operations
            tags: Additional metadata tags
        """
        self.span_id = str(uuid.uuid4())
        self.trace_id = trace_id_var.get() or str(uuid.uuid4())
        self.operation = operation
        self.parent_span_id = parent_span_id
        self.tags = tags or {}
        self.start_time = None
        self.end_time = None
        self.duration_ms = None
        self.status = "pending"
        self.logs = []
        
        # Set trace ID in context
        trace_id_var.set(self.trace_id)
    
    def __enter__(self):
        """Start span"""
        self.start_time = time.time()
        self.status = "active"
        
        self.log("span_started", {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "operation": self.operation
        })
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End span"""
        self.end_time = time.time()
        self.duration_ms = (self.end_time - self.start_time) * 1000
        
        if exc_type:
            self.status = "error"
            self.add_tag("error", True)
            self.add_tag("error_type", exc_type.__name__)
            self.add_tag("error_message", str(exc_val))
        else:
            self.status = "completed"
        
        self.log("span_completed", {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status
        })
        
        # Export span to tracing backend (e.g., Jaeger, Zipkin)
        self._export_span()
        
        return False  # Don't suppress exceptions
    
    def add_tag(self, key: str, value: Any):
        """Add tag to span"""
        self.tags[key] = value
    
    def log(self, event: str, data: Optional[Dict[str, Any]] = None):
        """Add log event to span"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event": event,
            "data": data or {}
        }
        self.logs.append(log_entry)
    
    def _export_span(self):
        """Export span to tracing backend"""
        span_data = {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "tags": self.tags,
            "logs": self.logs
        }
        
        # In production, send to Jaeger/Zipkin
        # For now, just log it
        logger = logging.getLogger("tracing")
        logger.debug(f"SPAN: {json.dumps(span_data)}")


def trace_operation(operation_name: str):
    """
    Decorator to automatically trace an operation
    
    Usage:
        @trace_operation("scale_up")
        def scale_up(self, count):
            # operation code
            pass
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with TracingSpan(operation_name) as span:
                # Add function metadata
                span.add_tag("function", func.__name__)
                span.add_tag("module", func.__module__)
                
                # Execute function
                result = func(*args, **kwargs)
                
                # Add result metadata if available
                if isinstance(result, dict):
                    if "success" in result:
                        span.add_tag("success", result["success"])
                
                return result
        
        return wrapper
    return decorator


class EnhancedMetrics:
    """
    Enhanced Prometheus metrics with better organization
    """
    
    def __init__(self):
        """Initialize enhanced metrics"""
        from prometheus_client import (
            Counter, Gauge, Histogram, Summary
        )
        
        # Scaling metrics
        self.scaling_decisions = Counter(
            'autoscaler_scaling_decisions_total',
            'Total scaling decisions',
            ['decision', 'reason']
        )
        
        self.scaling_duration = Histogram(
            'autoscaler_scaling_duration_seconds',
            'Time taken for scaling operations',
            ['action']
        )
        
        # Resource metrics
        self.current_nodes = Gauge(
            'autoscaler_current_nodes',
            'Current number of worker nodes'
        )
        
        self.pending_pods = Gauge(
            'autoscaler_pending_pods',
            'Number of pending pods'
        )
        
        self.cluster_cpu_usage = Gauge(
            'autoscaler_cluster_cpu_usage_percent',
            'Average CPU usage across cluster'
        )
        
        self.cluster_memory_usage = Gauge(
            'autoscaler_cluster_memory_usage_percent',
            'Average memory usage across cluster'
        )
        
        # Node-level metrics
        self.node_cpu_usage = Gauge(
            'autoscaler_node_cpu_usage_percent',
            'CPU usage per node',
            ['node_name']
        )
        
        self.node_memory_usage = Gauge(
            'autoscaler_node_memory_usage_percent',
            'Memory usage per node',
            ['node_name']
        )
        
        # Operation metrics
        self.api_requests = Counter(
            'autoscaler_api_requests_total',
            'Total API requests',
            ['endpoint', 'method', 'status']
        )
        
        self.api_latency = Summary(
            'autoscaler_api_latency_seconds',
            'API request latency',
            ['endpoint']
        )
        
        # Error metrics
        self.errors = Counter(
            'autoscaler_errors_total',
            'Total errors by type',
            ['error_type', 'component']
        )
        
        # Database metrics
        self.db_operations = Counter(
            'autoscaler_db_operations_total',
            'Database operations',
            ['operation', 'collection', 'status']
        )
        
        self.db_latency = Histogram(
            'autoscaler_db_latency_seconds',
            'Database operation latency',
            ['operation']
        )
        
        # Circuit breaker metrics
        self.circuit_breaker_state = Gauge(
            'autoscaler_circuit_breaker_state',
            'Circuit breaker state (0=closed, 1=open, 2=half_open)',
            ['circuit_name']
        )
        
        self.circuit_breaker_failures = Counter(
            'autoscaler_circuit_breaker_failures_total',
            'Circuit breaker failures',
            ['circuit_name']
        )
    
    def record_scaling_decision(self, decision: str, reason: str):
        """Record scaling decision"""
        self.scaling_decisions.labels(
            decision=decision,
            reason=reason
        ).inc()
    
    def record_scaling_duration(self, action: str, duration: float):
        """Record scaling operation duration"""
        self.scaling_duration.labels(action=action).observe(duration)
    
    def update_cluster_metrics(self, metrics: Dict[str, Any]):
        """Update cluster-level metrics"""
        self.current_nodes.set(metrics.get('current_nodes', 0))
        self.pending_pods.set(metrics.get('pending_pods', 0))
        self.cluster_cpu_usage.set(metrics.get('avg_cpu', 0))
        self.cluster_memory_usage.set(metrics.get('avg_memory', 0))
    
    def update_node_metrics(self, node_name: str, cpu: float, memory: float):
        """Update per-node metrics"""
        self.node_cpu_usage.labels(node_name=node_name).set(cpu)
        self.node_memory_usage.labels(node_name=node_name).set(memory)
    
    def record_api_request(
        self,
        endpoint: str,
        method: str,
        status: int,
        duration: float
    ):
        """Record API request"""
        self.api_requests.labels(
            endpoint=endpoint,
            method=method,
            status=status
        ).inc()
        
        self.api_latency.labels(endpoint=endpoint).observe(duration)
    
    def record_error(self, error_type: str, component: str):
        """Record error"""
        self.errors.labels(
            error_type=error_type,
            component=component
        ).inc()


# Usage example in autoscaler.py:
class AutoscalerWithObservability:
    """Autoscaler with enhanced observability"""
    
    def __init__(self, config, database):
        self.config = config
        self.database = database
        
        # Initialize observability
        self.logger = StructuredLogger(__name__, {
            "component": "autoscaler",
            "version": "1.0.0"
        })
        self.metrics = EnhancedMetrics()
    
    @trace_operation("autoscaling_cycle")
    def run_cycle(self):
        """Run autoscaling cycle with full observability"""
        cycle_start = time.time()
        
        try:
            self.logger.info("Starting autoscaling cycle")
            
            # Collect metrics with tracing
            with TracingSpan("collect_metrics") as span:
                metrics = self._collect_metrics()
                span.add_tag("node_count", metrics.get('current_nodes'))
                span.add_tag("pending_pods", metrics.get('pending_pods'))
            
            # Make scaling decision with tracing
            with TracingSpan("evaluate_scaling") as span:
                decision = self._evaluate_scaling(metrics)
                span.add_tag("should_scale", decision.get('should_scale'))
                span.add_tag("action", decision.get('action'))
            
            # Execute scaling if needed
            if decision.get('should_scale'):
                with TracingSpan("execute_scaling") as span:
                    span.add_tag("action", decision['action'])
                    success = self._execute_scaling(decision)
                    span.add_tag("success", success)
                    
                    # Record metrics
                    duration = time.time() - cycle_start
                    self.metrics.record_scaling_duration(
                        decision['action'],
                        duration
                    )
                    self.metrics.record_scaling_decision(
                        decision['action'],
                        decision.get('reason', 'unknown')
                    )
            
            # Update cluster metrics
            self.metrics.update_cluster_metrics(metrics)
            
            self.logger.info(
                "Autoscaling cycle completed",
                duration_ms=(time.time() - cycle_start) * 1000,
                decision=decision.get('action', 'no_action')
            )
            
            return {"success": True, "decision": decision}
            
        except Exception as e:
            self.logger.error(
                "Autoscaling cycle failed",
                error=e,
                duration_ms=(time.time() - cycle_start) * 1000
            )
            self.metrics.record_error(
                type(e).__name__,
                "autoscaler"
            )
            return {"success": False, "error": str(e)}
