#!/usr/bin/env python3
"""
Circuit Breaker Pattern Implementation
Prevents cascading failures when external services are down
"""

import logging
import time
from enum import Enum
from typing import Callable, Any, Optional
from datetime import datetime, timedelta
from functools import wraps

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failures detected, circuit open
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker to prevent cascading failures
    
    States:
    - CLOSED: Normal operation, all requests pass through
    - OPEN: Too many failures, all requests fail fast
    - HALF_OPEN: Testing if service recovered, limited requests pass
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
        name: str = "circuit_breaker"
    ):
        """
        Initialize circuit breaker
        
        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting recovery
            expected_exception: Exception type to catch
            name: Name of this circuit breaker
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.name = name
        
        # State tracking
        self.failure_count = 0
        self.last_failure_time: Optional[datetime] = None
        self.state = CircuitState.CLOSED
        
        logger.info(
            f"Circuit breaker '{name}' initialized: "
            f"threshold={failure_threshold}, timeout={recovery_timeout}s"
        )
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection
        
        Args:
            func: Function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func
            
        Returns:
            Result of func execution
            
        Raises:
            Exception: If circuit is open or func raises exception
        """
        # Check if we should attempt recovery
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit '{self.name}' entering HALF_OPEN state")
            else:
                raise Exception(
                    f"Circuit breaker '{self.name}' is OPEN. "
                    f"Service unavailable. Retry after {self.recovery_timeout}s"
                )
        
        try:
            # Execute the function
            result = func(*args, **kwargs)
            
            # Success - reset failure count if recovering
            if self.state == CircuitState.HALF_OPEN:
                self._on_success()
            
            return result
            
        except self.expected_exception as e:
            self._on_failure()
            raise
    
    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt recovery"""
        if not self.last_failure_time:
            return True
        
        return datetime.utcnow() >= (
            self.last_failure_time + timedelta(seconds=self.recovery_timeout)
        )
    
    def _on_success(self):
        """Handle successful execution"""
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        logger.info(f"Circuit '{self.name}' closed after successful recovery")
    
    def _on_failure(self):
        """Handle failed execution"""
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.error(
                f"Circuit '{self.name}' opened after {self.failure_count} failures"
            )
        else:
            logger.warning(
                f"Circuit '{self.name}' failure {self.failure_count}/"
                f"{self.failure_threshold}"
            )
    
    def reset(self):
        """Manually reset the circuit breaker"""
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = None
        logger.info(f"Circuit '{self.name}' manually reset")
    
    def get_state(self) -> dict:
        """Get current circuit breaker state"""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None
        }


# Decorator for easy circuit breaker application
def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
    name: Optional[str] = None
):
    """
    Decorator to apply circuit breaker to a function
    
    Usage:
        @circuit_breaker(failure_threshold=3, recovery_timeout=30)
        def call_external_api():
            # API call here
            pass
    """
    def decorator(func: Callable) -> Callable:
        breaker_name = name or f"{func.__module__}.{func.__name__}"
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            name=breaker_name
        )
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            return breaker.call(func, *args, **kwargs)
        
        # Attach breaker to wrapper for external access
        wrapper.circuit_breaker = breaker
        return wrapper
    
    return decorator


# Example usage in metrics.py:
class MetricsCollectorWithCircuitBreaker:
    """Metrics collector with circuit breaker protection"""
    
    def __init__(self, config):
        self.config = config
        
        # Circuit breakers for different services
        self.prometheus_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=30,
            name="prometheus"
        )
        
        self.k8s_breaker = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=30,
            name="kubernetes"
        )
    
    def _query_prometheus_safe(self, query: str):
        """Query Prometheus with circuit breaker protection"""
        return self.prometheus_breaker.call(
            self._query_prometheus,
            query
        )
    
    def _query_prometheus(self, query: str):
        """Actual Prometheus query implementation"""
        import requests
        response = requests.get(
            f"{self.config['prometheus']['url']}/api/v1/query",
            params={'query': query},
            timeout=5
        )
        response.raise_for_status()
        return response.json()
    
    def collect(self):
        """Collect metrics with circuit breaker protection"""
        try:
            # Try to get Prometheus metrics
            cpu_data = self._query_prometheus_safe(
                'avg(100 - (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
            )
        except Exception as e:
            logger.error(f"Prometheus unavailable: {e}")
            # Fall back to default values or cached metrics
            cpu_data = None
        
        try:
            # Try to get Kubernetes metrics
            k8s_data = self.k8s_breaker.call(self._get_k8s_metrics)
        except Exception as e:
            logger.error(f"Kubernetes API unavailable: {e}")
            k8s_data = None
        
        # Return best available data
        return {
            "prometheus_data": cpu_data,
            "k8s_data": k8s_data,
            "prometheus_available": self.prometheus_breaker.state == CircuitState.CLOSED,
            "k8s_available": self.k8s_breaker.state == CircuitState.CLOSED
        }


# Monitoring endpoint for circuit breaker status
def get_circuit_breaker_status(breakers: list[CircuitBreaker]) -> dict:
    """Get status of all circuit breakers"""
    return {
        "circuit_breakers": [breaker.get_state() for breaker in breakers],
        "timestamp": datetime.utcnow().isoformat()
    }
