# Implementation Guide for Key Enhancements

## 1. Async/Concurrent Operations

### Current Architecture Issues
```python
# Current: Sequential creation
def _scale_up(self, count: int):
    for i in range(count):
        worker = self._create_worker_node()  # Blocks for ~30s
        self._wait_for_kubernetes_node(worker)
```

### Proposed Async Implementation
```python
# File: autoscaler/src/core/async_scaling.py
import asyncio
import concurrent.futures
from typing import List, Optional

class AsyncScalingManager:
    def __init__(self, max_concurrent=3):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    async def scale_up_concurrent(self, count: int) -> List[WorkerNode]:
        """Scale up with parallel node creation"""
        tasks = [self._create_worker_async() for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter successful creations
        workers = [r for r in results if isinstance(r, WorkerNode)]
        errors = [r for r in results if isinstance(r, Exception)]

        if errors:
            logger.warning(f"Some nodes failed to create: {errors}")

        return workers

    async def _create_worker_async(self) -> WorkerNode:
        """Create worker in async context"""
        async with self.semaphore:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self.executor,
                self._create_worker_sync
            )
```

### Integration Points
```python
# In autoscaler.py
class K3sAutoscaler:
    def __init__(self, config, database):
        # ... existing init ...
        self.async_scaling = AsyncScalingManager(
            max_concurrent=config.get('max_concurrent_creations', 3)
        )

    async def scale_up_async(self, count: int) -> bool:
        """Async scale-up method"""
        try:
            workers = await self.async_scaling.scale_up_concurrent(count)

            # Verify nodes in parallel
            verification_tasks = [
                self._wait_for_kubernetes_node_async(w.node_name)
                for w in workers
            ]
            results = await asyncio.gather(*verification_tasks)

            # Update database with verified nodes
            for worker, verified in zip(workers, results):
                if verified:
                    self.database.add_worker(worker)

            return True
        except Exception as e:
            logger.error(f"Async scale-up failed: {e}")
            return False
```

## 2. Circuit Breaker Pattern

### Implementation
```python
# File: autoscaler/src/utils/circuit_breaker.py
import time
from enum import Enum
from functools import wraps

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.next_attempt = 0

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if self.state == CircuitState.OPEN:
                if time.time() < self.next_attempt:
                    raise Exception("Circuit breaker is OPEN")
                self.state = CircuitState.HALF_OPEN

            try:
                result = func(*args, **kwargs)
                self.on_success()
                return result
            except Exception as e:
                self.on_failure()
                raise e

        return wrapper

    def on_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def on_failure(self):
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.next_attempt = time.time() + self.recovery_timeout

# Usage:
@CircuitBreaker(failure_threshold=3, recovery_timeout=30)
def call_kubernetes_api(self):
    # API call logic
    pass
```

## 3. Enhanced Metrics Collection

### Custom Prometheus Metrics
```python
# File: autoscaler/src/metrics/custom.py
from prometheus_client import Gauge, Histogram, Counter

# Scaling metrics
SCALING_DECISION_LATENCY = Histogram(
    'autoscaler_scaling_decision_latency_seconds',
    'Time taken to make scaling decision',
    ['action']
)

NODE_READINESS_TIME = Histogram(
    'autoscaler_node_readiness_seconds',
    'Time for node to become ready',
    ['node_type']
)

SCALING_EVENTS_TOTAL = Counter(
    'autoscaler_scaling_events_total',
    'Total scaling events',
    ['action', 'result']
)

# Resource metrics
COST_PER_HOUR = Gauge(
    'autoscaler_cost_per_hour',
    'Current hourly cost of the cluster',
    ['currency']
)

RESOURCE_UTILIZATION = Gauge(
    'autoscaler_resource_utilization',
    'Resource utilization percentage',
    ['resource_type', 'node_name']
)

# Tracking decorator
def track_scaling_latency(action):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                SCALING_EVENTS_TOTAL.labels(
                    action=action,
                    result='success'
                ).inc()
                return result
            except Exception as e:
                SCALING_EVENTS_TOTAL.labels(
                    action=action,
                    result='failure'
                ).inc()
                raise
            finally:
                SCALING_DECISION_LATENCY.labels(action=action).observe(
                    time.time() - start
                )
        return wrapper
    return decorator

# Usage:
@track_scaling_latency('scale_up')
def _scale_up(self, count: int):
    # Scale-up logic
    pass
```

## 4. Time-based Scaling Policies

### Policy Engine
```python
# File: autoscaler/src/core/policies.py
from datetime import datetime, time as dt_time
from typing import Dict, Optional
import pytz

class TimeBasedPolicy:
    def __init__(self, policies: Dict):
        self.policies = policies
        self.timezone = pytz.UTC

    def get_active_policy(self, current_time: Optional[datetime] = None) -> Dict:
        """Get the active policy based on current time"""
        if current_time is None:
            current_time = datetime.now(self.timezone)

        current_hour = current_time.hour
        current_day = current_time.weekday()

        # Check each policy
        for policy_name, policy in self.policies.items():
            if self._matches_time(policy, current_hour, current_day):
                return policy

        # Return default policy if no match
        return self.policies.get('default', {})

    def _matches_time(self, policy: Dict, hour: int, day: int) -> bool:
        """Check if policy matches current time"""
        time_ranges = policy.get('time_ranges', [])
        days = policy.get('days', list(range(7)))  # Default all days

        if day not in days:
            return False

        for time_range in time_ranges:
            start, end = time_range.split('-')
            start_hour = int(start.split(':')[0])
            end_hour = int(end.split(':')[0])

            if start_hour <= hour < end_hour:
                return True

        return False

# In autoscaler.py:
def get_effective_limits(self) -> Dict:
    """Get effective limits based on time-based policies"""
    if hasattr(self, 'time_policy'):
        policy = self.time_policy.get_active_policy()
        return {
            'min_nodes': policy.get('min_nodes', self.config['autoscaler']['limits']['min_nodes']),
            'max_nodes': policy.get('max_nodes', self.config['autoscaler']['limits']['max_nodes']),
            'scale_up_threshold': policy.get('scale_up_threshold', 80),
            'scale_down_threshold': policy.get('scale_down_threshold', 30)
        }
    return self.config['autoscaler']['limits']
```

### Configuration Example
```yaml
# config/config-with-policies.yaml
autoscaler:
  time_based_policies:
    business_hours:
      time_ranges: ["09:00-17:00"]
      days: [0, 1, 2, 3, 4]  # Monday-Friday
      min_nodes: 3
      max_nodes: 8
      scale_up_threshold: 70
    weekend:
      time_ranges: ["00:00-23:59"]
      days: [5, 6]  # Saturday-Sunday
      min_nodes: 1
      max_nodes: 3
      scale_up_threshold: 85
    default:
      min_nodes: 2
      max_nodes: 6
      scale_up_threshold: 80
      scale_down_threshold: 30
```

## 5. Testing Framework

### Chaos Engineering Tests
```python
# File: tests/test_chaos.py
import pytest
import asyncio
from chaoslib import Action, Experiment
from autoscaler import K3sAutoscaler

class TestChaos:
    @pytest.mark.asyncio
    async def test_scale_up_during_api_failure(self, autoscaler):
        """Test scaling continues even if some APIs fail"""
        with Experiment("Scale-up with API failures") as exp:
            # Inject API failures
            exp.inject_action(kubernetes_api_failure, probability=0.3)
            exp.inject_action(container_creation_delay, delay=(5, 15))

            # Trigger scale-up
            result = await autoscaler.scale_up_async(3)

            # Verify at least 2 nodes created (some failures expected)
            assert len(result) >= 2

    @pytest.mark.asyncio
    async def test_node_failure_during_scale_up(self, autoscaler):
        """Test system recovers when nodes fail during scaling"""
        with Experiment("Node failure during scale-up"):
            # Start scale-up
            task = asyncio.create_task(autoscaler.scale_up_async(5))

            # Kill random nodes during scaling
            await asyncio.sleep(10)
            await self.kill_random_nodes(2)

            # Wait for completion
            result = await task

            # Verify final state is consistent
            assert autoscaler._verify_cluster_state()

# Integration Tests
class TestScalingIntegration:
    @pytest.mark.integration
    def test_full_scaling_cycle(self):
        """Test complete scaling: up -> stable -> down"""
        # Deploy test workload
        deploy_test_workload(replicas=50)

        # Verify scale-up
        wait_for_scale_up(target_nodes=5, timeout=300)

        # Reduce workload
        scale_workload(replicas=5)

        # Wait for scale-down
        wait_for_scale_down(target_nodes=2, timeout=300)

        # Verify all metrics
        verify_metrics_consistency()
```

## Integration Checklist

1. **Code Review**:
   - Review async implementation for race conditions
   - Validate circuit breaker logic
   - Check for proper error handling

2. **Testing**:
   - Unit tests for new components
   - Integration tests for full workflow
   - Load testing with concurrent operations
   - Chaos testing for failure scenarios

3. **Documentation**:
   - Update API documentation
   - Document new configuration options
   - Create troubleshooting guide
   - Add deployment instructions

4. **Monitoring**:
   - Add alerting rules for new metrics
   - Update Grafana dashboards
   - Set up health checks
   - Configure log aggregation

---

This guide provides concrete implementation details for the most impactful improvements. Each section includes code examples and integration patterns.