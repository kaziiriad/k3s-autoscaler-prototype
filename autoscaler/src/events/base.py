#!/usr/bin/env python3
"""
Base event classes for the event-driven autoscaler architecture
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    """Event types for the autoscaler"""

    # Core Events
    OPTIMAL_STATE_TRIGGER = "OptimalStateTrigger"
    SCALING_ACTION_COMPLETED = "ScalingActionCompleted"
    CLUSTER_STATE_SYNCED = "ClusterStateSynced"

    # Resource Events
    UNDERUTILIZATION_WARNING = "UnderutilizationWarning"
    RESOURCE_PRESSURE_ALERT = "ResourcePressureAlert"
    PENDING_PODS_DETECTED = "PendingPodsDetected"

    # Node Lifecycle Events
    NODE_HEALTH_DEGRADED = "NodeHealthDegraded"
    NODE_UNEXPECTEDLY_LOST = "NodeUnexpectedlyLost"
    NODE_CREATED = "NodeCreated"
    NODE_READY = "NodeReady"
    NODE_NOT_READY = "NodeNotReady"
    NODE_DRAINING = "NodeDraining"
    NODE_DRAINED = "NodeDrained"
    NODE_DELETED = "NodeDeleted"

    # Minimum Node Enforcement Events
    MINIMUM_NODES_VIOLATION = "MinimumNodesViolation"
    AUTO_SCALE_UP_TO_MINIMUM = "AutoScaleUpToMinimum"
    MINIMUM_NODES_RESTORED = "MinimumNodesRestored"
    EMERGENCY_SCALE_UP = "EmergencyScaleUp"

    # Decision Events
    SCALE_DOWN_BLOCKED = "ScaleDownBlocked"

    # Scaling Events
    SCALE_UP_DECISION = "ScaleUpDecision"
    SCALE_DOWN_DECISION = "ScaleDownDecision"
    SCALE_UP_STARTED = "ScaleUpStarted"
    SCALE_DOWN_STARTED = "ScaleDownStarted"
    SCALE_UP_COMPLETED = "ScaleUpCompleted"
    SCALE_DOWN_COMPLETED = "ScaleDownCompleted"

    # State Events
    AUTOSCALER_STARTED = "AutoscalerStarted"
    AUTOSCALER_STOPPED = "AutoscalerStopped"
    CONFIGURATION_CHANGED = "ConfigurationChanged"
    SCALING_RULE_ADDED = "ScalingRuleAdded"
    SCALING_RULE_MODIFIED = "ScalingRuleModified"
    SCALING_RULE_DELETED = "ScalingRuleDeleted"

    # Health Check Events
    NODE_HEALTH_CHECK_FAILED = "NodeHealthCheckFailed"

    # Cooldown Events
    SCALING_COOLDOWN_STARTED = "ScalingCooldownStarted"


@dataclass
class Event:
    """Base class for all autoscaler events"""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_type: EventType = field(init=False)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "autoscaler"
    data: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Set event_type from class name if not provided"""
        if not self.event_type:
            # Extract event type from class name
            class_name = self.__class__.__name__
            if class_name.endswith("Event"):
                class_name = class_name[:-5]  # Remove "Event" suffix
            # Convert to snake_case and upper case for enum
            event_type_str = ''.join(['_' + c.lower() if c.isupper() else c for c in class_name]).lstrip('_')
            self.event_type = EventType(event_type_str)

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "data": self.data,
            "metadata": self.metadata
        }

    def to_json(self) -> str:
        """Convert event to JSON string"""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Event':
        """Create event from dictionary"""
        # Extract event type
        event_type = data.get("event_type")
        if event_type:
            try:
                event_type = EventType(event_type)
            except ValueError:
                event_type = None

        # Create instance
        event = cls(
            event_id=data.get("event_id", str(uuid.uuid4())),
            timestamp=datetime.fromisoformat(data.get("timestamp", datetime.now(timezone.utc).isoformat())),
            source=data.get("source", "autoscaler"),
            data=data.get("data", {}),
            metadata=data.get("metadata", {})
        )

        if event_type:
            event.event_type = event_type

        return event

    @classmethod
    def from_json(cls, json_str: str) -> 'Event':
        """Create event from JSON string"""
        data = json.loads(json_str)
        return cls.from_dict(data)


class EventHandler:
    """Base class for event handlers"""

    def __init__(self, name: str):
        self.name = name
        self.subscribed_events: set[EventType] = set()

    async def handle(self, event: Event) -> bool:
        """
        Handle an event

        Args:
            event: The event to handle

        Returns:
            True if handled successfully, False otherwise
        """
        raise NotImplementedError("Subclasses must implement handle() method")

    def subscribe(self, event_type: EventType):
        """Subscribe to an event type"""
        self.subscribed_events.add(event_type)

    def unsubscribe(self, event_type: EventType):
        """Unsubscribe from an event type"""
        self.subscribed_events.discard(event_type)

    def is_subscribed(self, event_type: EventType) -> bool:
        """Check if subscribed to event type"""
        return event_type in self.subscribed_events