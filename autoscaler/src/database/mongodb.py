#!/usr/bin/env python3
"""
MongoDB schemas and models for the k3s autoscaler
"""

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import ConnectionFailure, DuplicateKeyError
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
import json
import os
from dataclasses import dataclass, asdict
from enum import Enum


class NodeStatus(str, Enum):
    """Worker node status enum"""
    INITIALIZING = "initializing"
    READY = "ready"
    DRAINING = "draining"
    REMOVING = "removing"
    REMOVED = "removed"
    ERROR = "error"


class ScalingEventType(str, Enum):
    """Scaling event types"""
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    NO_ACTION = "no_action"
    ERROR = "error"


@dataclass
class WorkerNode:
    """Worker node document schema"""
    node_name: str
    container_id: str
    container_name: str
    status: NodeStatus
    launched_at: datetime
    last_seen: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None
    instance_ip: Optional[str] = None
    kubelet_ready: bool = False
    id: Optional[str] = None  # MongoDB _id

    def to_dict(self) -> Dict[str, Any]:
        """Convert to MongoDB document"""
        return {
            "_id": self.node_name,
            "node_name": self.node_name,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "status": self.status.value,
            "launched_at": self.launched_at,
            "last_seen": self.last_seen,
            "metadata": self.metadata or {},
            "instance_ip": self.instance_ip,
            "kubelet_ready": self.kubelet_ready,
            "updated_at": datetime.utcnow()
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'WorkerNode':
        """Create from MongoDB document"""
        return cls(
            node_name=data.get("node_name"),
            container_id=data.get("container_id"),
            container_name=data.get("container_name"),
            status=NodeStatus(data.get("status")),
            launched_at=data.get("launched_at"),
            last_seen=data.get("last_seen"),
            metadata=data.get("metadata"),
            instance_ip=data.get("instance_ip"),
            kubelet_ready=data.get("kubelet_ready", False)
        )


@dataclass
class ScalingEvent:
    """Scaling event document schema"""
    event_type: ScalingEventType
    node_name: Optional[str]
    old_count: int
    new_count: int
    reason: Optional[str]
    metrics: Dict[str, Any]
    decision_details: Dict[str, Any]
    timestamp: datetime
    duration_ms: Optional[int] = None
    success: bool = True
    id: Optional[str] = None  # MongoDB _id

    def to_dict(self) -> Dict[str, Any]:
        """Convert to MongoDB document"""
        return {
            "event_type": self.event_type.value,
            "node_name": self.node_name,
            "old_count": self.old_count,
            "new_count": self.new_count,
            "reason": self.reason,
            "metrics": self.metrics,
            "decision_details": self.decision_details,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "success": self.success
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScalingEvent':
        """Create from MongoDB document"""
        return cls(
            event_type=ScalingEventType(data.get("event_type")),
            node_name=data.get("node_name"),
            old_count=data.get("old_count"),
            new_count=data.get("new_count"),
            reason=data.get("reason"),
            metrics=data.get("metrics", {}),
            decision_details=data.get("decision_details", {}),
            timestamp=data.get("timestamp"),
            duration_ms=data.get("duration_ms"),
            success=data.get("success", True)
        )


@dataclass
class ClusterState:
    """Cluster state document schema"""
    key: str
    value: Any
    value_type: str  # string, int, float, json, bool
    expires_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to MongoDB document"""
        return {
            "_id": self.key,
            "key": self.key,
            "value": self.value,
            "value_type": self.value_type,
            "expires_at": self.expires_at,
            "updated_at": self.updated_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ClusterState':
        """Create from MongoDB document"""
        # Parse value based on type
        value = data.get("value")
        if data.get("value_type") == "json" and isinstance(value, str):
            try:
                value = json.loads(value)
            except:
                pass

        return cls(
            key=data.get("key"),
            value=value,
            value_type=data.get("value_type", "string"),
            expires_at=data.get("expires_at"),
            updated_at=data.get("updated_at")
        )


@dataclass
class ScalingRule:
    """Scaling rule configuration schema"""
    name: str
    enabled: bool
    metric_name: str  # cpu, memory, pending_pods, custom
    operator: str  # >, <, >=, <=, ==
    threshold: float
    scale_direction: str  # up, down
    scale_amount: int
    cooldown_seconds: int
    conditions: Dict[str, Any]  # Additional conditions
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    id: Optional[str] = None  # MongoDB _id

    def to_dict(self) -> Dict[str, Any]:
        """Convert to MongoDB document"""
        return {
            "_id": self.name,
            "name": self.name,
            "enabled": self.enabled,
            "metric_name": self.metric_name,
            "operator": self.operator,
            "threshold": self.threshold,
            "scale_direction": self.scale_direction,
            "scale_amount": self.scale_amount,
            "cooldown_seconds": self.cooldown_seconds,
            "conditions": self.conditions,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScalingRule':
        """Create from MongoDB document"""
        return cls(
            name=data.get("name"),
            enabled=data.get("enabled", True),
            metric_name=data.get("metric_name"),
            operator=data.get("operator"),
            threshold=data.get("threshold"),
            scale_direction=data.get("scale_direction"),
            scale_amount=data.get("scale_amount", 1),
            cooldown_seconds=data.get("cooldown_seconds", 300),
            conditions=data.get("conditions", {}),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at")
        )