#!/usr/bin/env python3
"""
Database manager that coordinates MongoDB and Redis
"""

import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta

from .repositories import (
    WorkerNodeRepository,
    ScalingEventRepository,
    ClusterStateRepository,
    ScalingRuleRepository
)
from .mongodb import WorkerNode, ScalingEvent, ClusterState, ScalingRule, NodeStatus, ScalingEventType
from .redis_client import AutoscalerRedisClient

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Coordinates MongoDB and Redis operations for the autoscaler"""

    def __init__(self, mongodb_url: str = "mongodb://localhost:27017",
                 database_name: str = "autoscaler",
                 redis_host: str = "localhost",
                 redis_port: int = 6379,
                 redis_db: int = 0,
                 redis_password: Optional[str] = None):
        """
        Initialize database manager

        Args:
            mongodb_url: MongoDB connection string
            database_name: MongoDB database name
            redis_host: Redis host
            redis_port: Redis port
            redis_db: Redis database number
            redis_password: Redis password
        """
        self.mongodb_url = mongodb_url
        self.database_name = database_name
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.redis_db = redis_db
        self.redis_password = redis_password

        # Repository instances
        self.workers = None
        self.events = None
        self.state = None
        self.rules = None

        # Redis client
        self.redis = None

        self.connect()

    def connect(self):
        """Connect to MongoDB and Redis"""
        try:
            # Connect to MongoDB
            self.workers = WorkerNodeRepository(self.mongodb_url, self.database_name)
            self.events = ScalingEventRepository(self.mongodb_url, self.database_name)
            self.state = ClusterStateRepository(self.mongodb_url, self.database_name)
            self.rules = ScalingRuleRepository(self.mongodb_url, self.database_name)

            # Connect to Redis
            self.redis = AutoscalerRedisClient(
                host=self.redis_host,
                port=self.redis_port,
                db=self.redis_db,
                password=self.redis_password
            )

            logger.info("Database manager connected successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to databases: {e}")
            return False

    def close(self):
        """Close all database connections"""
        try:
            if self.workers:
                self.workers.close()
            if self.redis:
                self.redis.close()
            logger.info("Database connections closed")
        except Exception as e:
            logger.error(f"Error closing database connections: {e}")

    # Worker Node Operations
    def add_worker(self, node: WorkerNode) -> bool:
        """Add a new worker node"""
        # Save to MongoDB
        if self.workers.save(node):
            # Set in Redis for quick access
            self.redis.set_worker_status(node.node_name, node.status.value)
            if node.metadata:
                self.redis.set_worker_metadata(node.node_name, node.metadata)
            # Record event
            self.record_scaling_event(
                ScalingEventType.SCALE_UP,
                old_count=self.get_worker_count(),
                new_count=self.get_worker_count() + 1,
                reason=f"Added worker {node.node_name}",
                node_name=node.node_name
            )
            return True
        return False

    def remove_worker(self, node_name: str) -> bool:
        """Remove a worker node"""
        worker = self.workers.get_by_name(node_name)
        if worker:
            # Update status in MongoDB
            self.workers.update_status(node_name, NodeStatus.REMOVING)
            # Update Redis
            self.redis.set_worker_status(node_name, NodeStatus.REMOVING.value)

            # Drain node
            self.redis.set_worker_status(node_name, NodeStatus.DRAINING.value)

            # Delete after some time
            # In production, you'd wait for pods to be drained

            # Delete from MongoDB
            if self.workers.delete(node_name):
                # Clear from Redis
                self.redis.delete(f"workers:{node_name}")
                # Record event
                self.record_scaling_event(
                    ScalingEventType.SCALE_DOWN,
                    old_count=self.get_worker_count(),
                    new_count=self.get_worker_count() - 1,
                    reason=f"Removed worker {node_name}",
                    node_name=node_name
                )
                return True
        return False

    def get_worker(self, node_name: str) -> Optional[WorkerNode]:
        """Get a worker node"""
        # Try Redis first for speed
        status = self.redis.get_worker_status(node_name)
        if status:
            # Get full data from MongoDB
            return self.workers.get_by_name(node_name)
        return None

    def get_all_workers(self) -> List[WorkerNode]:
        """Get all worker nodes"""
        return self.workers.get_active_nodes()

    def get_ready_workers(self) -> List[WorkerNode]:
        """Get all ready workers"""
        return self.workers.get_ready_nodes()

    def get_worker_count(self) -> int:
        """Get worker node count"""
        return self.workers.get_count()

    def update_worker_status(self, node_name: str, status: NodeStatus) -> bool:
        """Update worker status"""
        if self.workers.update_status(node_name, status):
            self.redis.set_worker_status(node_name, status.value)
            return True
        return False

    def update_worker_last_seen(self, node_name: str) -> bool:
        """Update worker last seen timestamp"""
        if self.workers.update_last_seen(node_name):
            return True
        return False

    # Scaling Event Operations
    def record_scaling_event(self, event_type: ScalingEventType,
                            old_count: int, new_count: int,
                            reason: str = None,
                            node_name: str = None,
                            metrics: Optional[Dict] = None,
                            details: Optional[Dict] = None) -> bool:
        """Record a scaling event"""
        event = ScalingEvent(
            event_type=event_type,
            node_name=node_name,
            old_count=old_count,
            new_count=new_count,
            reason=reason,
            metrics=metrics or {},
            decision_details=details or {},
            timestamp=datetime.utcnow()
        )
        return self.events.create(event)

    def get_recent_events(self, limit: int = 100) -> List[ScalingEvent]:
        """Get recent scaling events"""
        return self.events.get_recent(limit)

    def get_last_scale_up(self) -> Optional[datetime]:
        """Get last scale-up timestamp"""
        timestamp = self.events.get_last_scale_up()
        # Also check Redis for faster access
        cached = self.redis.get("last_scale_up")
        if cached and timestamp:
            return max(timestamp, datetime.fromisoformat(cached))
        return timestamp

    def get_last_scale_down(self) -> Optional[datetime]:
        """Get last scale-down timestamp"""
        timestamp = self.events.get_last_scale_down()
        # Also check Redis for faster access
        cached = self.redis.get("last_scale_down")
        if cached and timestamp:
            return max(timestamp, datetime.fromisoformat(cached))
        return timestamp

    # Cluster State Operations
    def set_cluster_state(self, key: str, value: Any, value_type: str = "string",
                           expire_in_seconds: Optional[int] = None) -> bool:
        """Set cluster state"""
        # Save to MongoDB
        if self.state.set(key, value, value_type, expire_in_seconds):
            # Cache in Redis if no expiration
            if not expire_in_seconds:
                self.redis.set(key, value, serialize=False)
            return True
        return False

    def get_cluster_state(self, key: str, default: Any = None) -> Any:
        """Get cluster state"""
        # Try Redis first
        value = self.redis.get(key, deserialize=False)
        if value is not None:
            return value
        # Fallback to MongoDB
        return self.state.get(key, default)

    def increment_metric(self, metric: str) -> int:
        """Increment a metric counter"""
        # Update in Redis (fast)
        count = self.redis.increment(metric)
        # Periodically sync to MongoDB
        if count % 10 == 0:  # Sync every 10 increments
            self.state.set(metric, count, "int")
        return count

    # Cooldown Management
    def set_cooldown(self, action: str, seconds: int):
        """Set cooldown timer"""
        self.redis.set_cooldown(action, seconds)

    def is_cooldown_active(self, action: str) -> bool:
        """Check if cooldown is active"""
        return self.redis.is_cooldown_active(action)

    def get_cooldown_remaining(self, action: str) -> int:
        """Get remaining cooldown time"""
        return self.redis.get_cooldown_remaining(action)

    # Metrics Caching
    def cache_metrics(self, metrics: Dict, expire: int = 10):
        """Cache cluster metrics"""
        self.redis.cache_metrics(metrics, expire)

    def get_cached_metrics(self) -> Optional[Dict]:
        """Get cached metrics"""
        return self.redis.get_cached_metrics()

    # Health Check Operations
    def record_health_check(self, node_name: str, check_type: str, status: str,
                            message: str = "", response_time: Optional[float] = None):
        """Record health check"""
        self.redis.record_health_check(node_name, check_type, status, message, response_time)

    def get_node_health(self, node_name: str) -> Dict:
        """Get all health checks for a node"""
        return self.redis.get_health_status(node_name)

    # Scaling Rules
    def save_scaling_rule(self, rule: ScalingRule) -> bool:
        """Save a scaling rule"""
        return self.rules.save(rule)

    def get_scaling_rules(self, enabled_only: bool = True) -> List[ScalingRule]:
        """Get scaling rules"""
        return self.rules.get_all(enabled_only)

    # Cleanup Operations
    def cleanup_old_data(self, events_days: int = 30, nodes_days: int = 7) -> Dict[str, int]:
        """Clean up old data"""
        results = {}
        results["events_deleted"] = self.events.cleanup_old_events(events_days)
        results["nodes_deleted"] = self.workers.cleanup_old_nodes(nodes_days)
        return results

    # System Health
    def check_database_health(self) -> Dict[str, bool]:
        """Check database connectivity"""
        health = {
            "mongodb": False,
            "redis": False
        }

        try:
            # Check MongoDB
            self.workers.collection.count_documents({})
            health["mongodb"] = True
        except Exception as e:
            logger.error(f"MongoDB health check failed: {e}")

        try:
            # Check Redis
            self.redis.client.ping()
            health["redis"] = True
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")

        return health

    def initialize_default_rules(self):
        """Initialize default scaling rules"""
        default_rules = [
            ScalingRule(
                name="cpu_scale_up",
                enabled=True,
                metric_name="cpu",
                operator=">",
                threshold=80.0,
                scale_direction="up",
                scale_amount=1,
                cooldown_seconds=60,
                conditions={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            ),
            ScalingRule(
                name="cpu_scale_down",
                enabled=True,
                metric_name="cpu",
                operator="<",
                threshold=30.0,
                scale_direction="down",
                scale_amount=1,
                cooldown_seconds=120,
                conditions={"min_nodes": 2},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            ),
            ScalingRule(
                name="memory_scale_up",
                enabled=True,
                metric_name="memory",
                operator=">",
                threshold=80.0,
                scale_direction="up",
                scale_amount=1,
                cooldown_seconds=60,
                conditions={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            ),
            ScalingRule(
                name="memory_scale_down",
                enabled=True,
                metric_name="memory",
                operator="<",
                threshold=30.0,
                scale_direction="down",
                scale_amount=1,
                cooldown_seconds=120,
                conditions={"min_nodes": 2},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            ),
            ScalingRule(
                name="pending_pods_scale_up",
                enabled=True,
                metric_name="pending_pods",
                operator=">=",
                threshold=1,
                scale_direction="up",
                scale_amount=1,
                cooldown_seconds=30,
                conditions={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
        ]

        for rule in default_rules:
            self.rules.save(rule)

        logger.info(f"Initialized {len(default_rules)} default scaling rules")