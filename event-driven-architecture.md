# Event-Driven Architecture for Autoscaler

## Overview
This document outlines the proposed event-driven architecture for the k3s autoscaler to improve debugging, visibility, and automatic recovery capabilities.

## Proposed Events

### Core Events

#### 1. **OptimalStateTrigger**
- **Trigger**: When worker count reaches minimum (2 nodes) AND resources are underutilized
- **Purpose**: Signals the system is in an optimal/efficient state
- **Data**: `{current_workers: 2, min_workers: 2, cpu_avg: 15%, memory_avg: 65%}`

#### 2. **ScalingActionCompleted**
- **Trigger**: After any scale-up or scale-down operation
- **Purpose**: Confirmation with details
- **Data**: `{action: "scale_up", count: 1, previous_workers: 2, new_workers: 3, duration: 45s}`

#### 3. **ClusterStateSynced**
- **Trigger**: When actual cluster state matches desired state
- **Purpose**: System stability indicator
- **Data**: `{k8s_nodes: 3, docker_containers: 3, db_workers: 3, last_sync: "2025-12-16T11:20:00Z"}`

### Resource Events

#### 4. **UnderutilizationWarning**
- **Trigger**: When workers > min AND CPU < 30% AND Memory < 40% for consecutive periods
- **Purpose**: Early warning before considering scale-down
- **Data**: `{workers: 3, min_workers: 2, cpu_avg: 25%, memory_avg: 35%, duration: 300s}`

#### 5. **ResourcePressureAlert**
- **Trigger**: When CPU > 80% OR Memory > 85% OR both
- **Purpose**: Immediate scale-up trigger
- **Data**: `{cpu_avg: 85%, memory_avg: 87%, workers: 3, max_workers: 6}`

#### 6. **PendingPodsDetected**
- **Trigger**: When pods are pending scheduling
- **Purpose**: Clear signal for scale-up need
- **Data**: `{pending_count: 3, waiting_time: 60s, affected_deployments: ["app1", "app2"]}`

### Node Lifecycle Events

#### 7. **NodeHealthDegraded**
- **Trigger**: When a node fails health checks or becomes NotReady
- **Purpose**: Proactive health management
- **Data**: `{node_name: "k3s-worker-3", status: "NotReady", last_check: "2025-12-16T11:20:00Z"}`

#### 8. **NodeUnexpectedlyLost**
- **Trigger**: When a node disappears without scale-down command
- **Data**: `{node_name: "k3s-worker-3", last_seen: "2025-12-16T11:19:00Z", cause: "container_crash"}`

### Minimum Node Enforcement Events

#### 9. **MinimumNodesViolation**
- **Trigger**: When worker count < min_workers (e.g., goes from 2 to 1)
- **Purpose**: Critical alert - below minimum capacity
- **Data**: `{current_workers: 1, min_workers: 2, violation_time: "2025-12-16T11:20:00Z"}`

#### 10. **AutoScaleUpToMinimum**
- **Trigger**: Immediately after MinimumNodesViolation
- **Purpose**: Automatic corrective action
- **Data**: `{current_workers: 1, min_workers: 2, scale_up_count: 1, reason: "minimum_violation"}`

#### 11. **MinimumNodesRestored**
- **Trigger**: When workers return to min_workers (e.g., back to 2)
- **Purpose**: Confirmation of recovery
- **Data**: `{current_workers: 2, min_workers: 2, restore_time: "2025-12-16T11:21:30Z"}`

#### 12. **EmergencyScaleUp**
- **Trigger**: When multiple nodes lost simultaneously
- **Data**: `{lost_count: 2, remaining_workers: 1, min_workers: 2, emergency_scale: 2}`

### Decision Events

#### 13. **ScaleDownBlocked**
- **Trigger**: When scale-down should happen but can't (e.g., pending pods, health issues)
- **Purpose**: Explains why scaling isn't happening when expected
- **Data**: `{reason: "pending_pods", pending_count: 5, workers: 4, min_workers: 2}`

## Event Flow Examples

### Scale-down scenario with better visibility:
```
1. ClusterStateSynced (workers=3)
2. UnderutilizationWarning (cpu=25%, memory=35%, duration=300s)
3. OptimalStateTrigger (would trigger at workers=2)
4. ScaleDownDecision (scale_down to 2)
5. ScaleDownStarted
6. ScalingActionCompleted (workers=2)
7. OptimalStateTrigger (active)
8. ClusterStateSynced (workers=2)
```

### Minimum node violation recovery:
```
1. NodeUnexpectedlyLost (k3s-worker-3 crashed)
2. MinimumNodesViolation (workers=1, min=2)
3. AutoScaleUpToMinimum (scale_up by 1)
4. ScaleUpStarted
5. NodeCreated (k3s-worker-14)
6. NodeReady (k3s-worker-14)
7. ScalingActionCompleted (workers=2)
8. MinimumNodesRestored (workers=2, min=2)
9. ClusterStateSynced (workers=2)
```

## Implementation Components

### 1. EventBus
- Central event dispatcher
- Supports pub/sub pattern
- Event filtering and routing
- Retry mechanisms for failed handlers
- Implementation: Redis Streams (using existing Redis infrastructure)

### 2. Event Structure
```python
{
    "event_id": "uuid",
    "event_type": "OptimalStateTrigger",
    "timestamp": "2025-12-16T11:20:00Z",
    "source": "metrics_collector",
    "data": {...},
    "metadata": {
        "cluster_id": "cluster-1",
        "region": "us-west-2"
    }
}
```

### 3. Event Handlers
- **MetricsListener** - Processes metrics-related events
- **ScalingDecisionEngine** - Handles threshold events
- **NodeManager** - Manages node lifecycle
- **MinimumNodeEnforcer** - Ensures minimum node count
- **StateSynchronizer** - Maintains cache/database consistency
- **AlertManager** - Sends notifications for critical events

### 4. MinimumNodeEnforcer Service
```python
class MinimumNodeEnforcer:
    """Ensures cluster never goes below minimum worker count"""

    def __init__(self, min_workers: int, event_bus: EventBus):
        self.min_workers = min_workers
        self.event_bus = event_bus

    async def check_minimum_nodes(self, current_workers: int):
        """Check if we're below minimum and trigger scale-up"""
        if current_workers < self.min_workers:
            # Emit critical violation event
            await self.event_bus.emit(MinimumNodesViolation(...))
            # Immediately trigger scale-up to minimum
            await self.event_bus.emit(AutoScaleUpToMinimum(...))
            return True
        return False
```

## Configuration

```yaml
autoscaler:
  limits:
    min_nodes: 2
    max_nodes: 6
  enforcement:
    auto_scale_to_min: true
    min_violation_check_interval: 10  # seconds
    emergency_scale_threshold: 2  # scale up 2x if this many lost
  events:
    event_bus:
      type: "redis_streams"
      stream_name: "autoscaler_events"
      max_stream_length: 10000
    handlers:
      - "MetricsListener"
      - "ScalingDecisionEngine"
      - "NodeManager"
      - "MinimumNodeEnforcer"
      - "StateSynchronizer"
```

## Benefits

1. **Clear Debugging**: Each scaling decision has explicit events explaining why
2. **State Visibility**: Always know what the system thinks is happening
3. **Self-Healing**: Automatic recovery from node failures
4. **Proactive Alerts**: Warn before problems occur
5. **Audit Trail**: Complete history of all state changes
6. **Loose Coupling**: Components communicate via events, not direct calls
7. **Extensibility**: Easy to add new event handlers
8. **Resilience**: Failed events can be retried or dead-lettered

## Implementation Priority

### Phase 1: Core Infrastructure
1. Event base classes and types
2. EventBus using Redis streams
3. Core events: OptimalStateTrigger, ScalingActionCompleted, ClusterStateSynced

### Phase 2: Minimum Node Enforcement
4. Minimum node enforcement events
5. MinimumNodeEnforcer service
6. Integration with existing scaling system

### Phase 3: Enhanced Visibility
7. Resource events (UnderutilizationWarning, ResourcePressureAlert)
8. Node lifecycle events
9. Event listeners for better logging and debugging

### Phase 4: Advanced Features
10. Event aggregation and metrics
11. Event replay capabilities
12. Webhook integrations for external notifications

## Next Steps

1. Create new branch: `feature/event-driven-architecture`
2. Implement event infrastructure
3. Add events to existing autoscaler code
4. Test with scale-up/scale-down scenarios
5. Verify minimum node enforcement works correctly

## Testing Scenarios

1. **Normal scale-down**: Verify OptimalStateTrigger fires at minimum workers
2. **Node failure**: Test MinimumNodesViolation and auto-recovery
3. **Resource pressure**: Verify ResourcePressureAlert triggers scale-up
4. **Pending pods**: Verify PendingPodsDetected triggers immediate scale-up
5. **State sync**: Verify ClusterStateSynced reflects accurate cluster state