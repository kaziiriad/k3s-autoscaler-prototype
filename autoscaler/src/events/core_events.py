#!/usr/bin/env python3
"""
Core event classes for the autoscaler
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from .base import Event, EventType


@dataclass
class OptimalStateTrigger(Event):
    """Event triggered when cluster is in optimal state"""
    event_type: EventType = EventType.OPTIMAL_STATE_TRIGGER

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "current_workers" not in self.data:
            raise ValueError("current_workers is required")
        if "min_workers" not in self.data:
            raise ValueError("min_workers is required")


@dataclass
class ScalingActionCompleted(Event):
    """Event triggered when scaling action completes"""
    event_type: EventType = EventType.SCALING_ACTION_COMPLETED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "action" not in self.data:
            raise ValueError("action is required")
        if "previous_workers" not in self.data:
            raise ValueError("previous_workers is required")
        if "new_workers" not in self.data:
            raise ValueError("new_workers is required")


@dataclass
class ClusterStateSynced(Event):
    """Event triggered when cluster state is synchronized"""
    event_type: EventType = EventType.CLUSTER_STATE_SYNCED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "k8s_nodes" not in self.data:
            raise ValueError("k8s_nodes is required")
        if "docker_containers" not in self.data:
            raise ValueError("docker_containers is required")
        if "db_workers" not in self.data:
            raise ValueError("db_workers is required")


@dataclass
class UnderutilizationWarning(Event):
    """Event triggered when cluster is underutilized"""
    event_type: EventType = EventType.UNDERUTILIZATION_WARNING

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "workers" not in self.data:
            raise ValueError("workers is required")
        if "cpu_avg" not in self.data:
            raise ValueError("cpu_avg is required")
        if "memory_avg" not in self.data:
            raise ValueError("memory_avg is required")


@dataclass
class ResourcePressureAlert(Event):
    """Event triggered when resources are under pressure"""
    event_type: EventType = EventType.RESOURCE_PRESSURE_ALERT

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "cpu_avg" not in self.data:
            raise ValueError("cpu_avg is required")
        if "memory_avg" not in self.data:
            raise ValueError("memory_avg is required")
        if "workers" not in self.data:
            raise ValueError("workers is required")


@dataclass
class PendingPodsDetected(Event):
    """Event triggered when pods are pending scheduling"""
    event_type: EventType = EventType.PENDING_PODS_DETECTED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "pending_count" not in self.data:
            raise ValueError("pending_count is required")


@dataclass
class NodeHealthDegraded(Event):
    """Event triggered when node health degrades"""
    event_type: EventType = EventType.NODE_HEALTH_DEGRADED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "node_name" not in self.data:
            raise ValueError("node_name is required")
        if "status" not in self.data:
            raise ValueError("status is required")


@dataclass
class MinimumNodesViolation(Event):
    """Event triggered when worker count goes below minimum"""
    event_type: EventType = EventType.MINIMUM_NODES_VIOLATION

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "current_workers" not in self.data:
            raise ValueError("current_workers is required")
        if "min_workers" not in self.data:
            raise ValueError("min_workers is required")


@dataclass
class AutoScaleUpToMinimum(Event):
    """Event triggered to auto-scale up to minimum workers"""
    event_type: EventType = EventType.AUTO_SCALE_UP_TO_MINIMUM

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "current_workers" not in self.data:
            raise ValueError("current_workers is required")
        if "min_workers" not in self.data:
            raise ValueError("min_workers is required")
        if "scale_up_count" not in self.data:
            raise ValueError("scale_up_count is required")


@dataclass
class MinimumNodesRestored(Event):
    """Event triggered when minimum workers are restored"""
    event_type: EventType = EventType.MINIMUM_NODES_RESTORED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "current_workers" not in self.data:
            raise ValueError("current_workers is required")
        if "min_workers" not in self.data:
            raise ValueError("min_workers is required")


@dataclass
class EmergencyScaleUp(Event):
    """Event triggered for emergency scale-up"""
    event_type: EventType = EventType.EMERGENCY_SCALE_UP

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "lost_count" not in self.data:
            raise ValueError("lost_count is required")
        if "remaining_workers" not in self.data:
            raise ValueError("remaining_workers is required")
        if "emergency_scale" not in self.data:
            raise ValueError("emergency_scale is required")


@dataclass
class ScaleDownBlocked(Event):
    """Event triggered when scale-down is blocked"""
    event_type: EventType = EventType.SCALE_DOWN_BLOCKED

    def __post_init__(self):
        super().__post_init__()
        # Validate required data
        if "reason" not in self.data:
            raise ValueError("reason is required")
        if "workers" not in self.data:
            raise ValueError("workers is required")
        if "min_workers" not in self.data:
            raise ValueError("min_workers is required")