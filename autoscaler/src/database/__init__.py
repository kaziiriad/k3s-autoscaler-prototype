"""
Database package for the k3s autoscaler

Provides MongoDB and Redis integration for persistent state management.
"""

from .mongodb import (
    WorkerNode,
    ScalingEvent,
    ClusterState,
    ScalingRule,
    NodeStatus,
    ScalingEventType
)

from .repositories import (
    WorkerNodeRepository,
    ScalingEventRepository,
    ClusterStateRepository,
    ScalingRuleRepository
)

from .redis_client import RedisClient, AutoscalerRedisClient
from .manager import DatabaseManager

__all__ = [
    # Schemas
    "WorkerNode",
    "ScalingEvent",
    "ClusterState",
    "ScalingRule",
    "NodeStatus",
    "ScalingEventType",

    # Repositories
    "WorkerNodeRepository",
    "ScalingEventRepository",
    "ClusterStateRepository",
    "ScalingRuleRepository",

    # Clients
    "RedisClient",
    "AutoscalerRedisClient",

    # Manager
    "DatabaseManager"
]