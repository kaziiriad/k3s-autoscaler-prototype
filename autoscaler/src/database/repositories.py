#!/usr/bin/env python3
"""
MongoDB repository classes for the k3s autoscaler
"""

from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, DuplicateKeyError
from pymongo.collection import Collection
import logging

from .mongodb import WorkerNode, ScalingEvent, ClusterState, ScalingRule, NodeStatus, ScalingEventType

logger = logging.getLogger(__name__)


class MongoDBRepository:
    """Base repository class with common MongoDB operations"""

    def __init__(self, connection_string: str = "mongodb://localhost:27017", database_name: str = "autoscaler"):
        self.connection_string = connection_string
        self.database_name = database_name
        self.client = None
        self.db = None
        self.connect()

    def connect(self):
        """Establish MongoDB connection"""
        try:
            self.client = MongoClient(self.connection_string)
            # Test connection
            self.client.admin.command('ping')
            self.db = self.client[self.database_name]
            logger.info(f"Connected to MongoDB: {self.database_name}")
            self._create_indexes()
        except ConnectionFailure as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise

    def close(self):
        """Close MongoDB connection"""
        if self.client:
            self.client.close()
            logger.info("MongoDB connection closed")

    def _create_indexes(self):
        """Create necessary indexes"""
        # Worker nodes indexes
        self.db.workers.create_index([("status", ASCENDING)])
        self.db.workers.create_index([("launched_at", DESCENDING)])

        # Scaling events indexes
        self.db.scaling_events.create_index([("timestamp", DESCENDING)])
        self.db.scaling_events.create_index([("event_type", ASCENDING)])

        # Cluster state indexes
        self.db.cluster_state.create_index([("key", ASCENDING)])
        self.db.cluster_state.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

        logger.info("MongoDB indexes created")


class WorkerNodeRepository(MongoDBRepository):
    """Repository for worker node operations"""

    @property
    def collection(self) -> Collection:
        return self.db.workers

    def save(self, worker: WorkerNode) -> bool:
        """Save or update a worker node"""
        try:
            self.collection.replace_one(
                {"_id": worker.node_name},
                worker.to_dict(),
                upsert=True
            )
            logger.debug(f"Saved worker node: {worker.node_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to save worker {worker.node_name}: {e}")
            return False

    def get_by_name(self, node_name: str) -> Optional[WorkerNode]:
        """Get a worker node by name"""
        doc = self.collection.find_one({"_id": node_name})
        if doc:
            return WorkerNode.from_dict(doc)
        return None

    def get_all(self, status: Optional[NodeStatus] = None) -> List[WorkerNode]:
        """Get all worker nodes, optionally filtered by status"""
        query = {}
        if status:
            query["status"] = status.value

        docs = self.collection.find(query).sort("launched_at", ASCENDING)
        return [WorkerNode.from_dict(doc) for doc in docs]

    def get_active_nodes(self) -> List[WorkerNode]:
        """Get all active nodes (not removed)"""
        return self.get_all().filter(
            lambda w: w.status not in [NodeStatus.REMOVED, NodeStatus.ERROR]
        )

    def get_ready_nodes(self) -> List[WorkerNode]:
        """Get all ready nodes"""
        return self.get_all(NodeStatus.READY)

    def update_status(self, node_name: str, status: NodeStatus) -> bool:
        """Update worker node status"""
        try:
            result = self.collection.update_one(
                {"_id": node_name},
                {
                    "$set": {
                        "status": status.value,
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update status for {node_name}: {e}")
            return False

    def update_last_seen(self, node_name: str) -> bool:
        """Update last seen timestamp"""
        try:
            result = self.collection.update_one(
                {"_id": node_name},
                {"$set": {"last_seen": datetime.utcnow()}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Failed to update last_seen for {node_name}: {e}")
            return False

    def delete(self, node_name: str) -> bool:
        """Delete a worker node"""
        try:
            result = self.collection.delete_one({"_id": node_name})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete worker {node_name}: {e}")
            return False

    def cleanup_old_nodes(self, days: int = 7) -> int:
        """Clean up old removed nodes"""
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        result = self.collection.delete_many({
            "status": NodeStatus.REMOVED.value,
            "updated_at": {"$lt": cutoff_date}
        })
        return result.deleted_count

    def get_count(self, status: Optional[NodeStatus] = None) -> int:
        """Get count of worker nodes, optionally filtered by status"""
        query = {}
        if status:
            query["status"] = status.value
        return self.collection.count_documents(query)


class ScalingEventRepository(MongoDBRepository):
    """Repository for scaling event operations"""

    @property
    def collection(self) -> Collection:
        return self.db.scaling_events

    def create(self, event: ScalingEvent) -> bool:
        """Create a new scaling event"""
        try:
            doc = event.to_dict()
            doc["_id"] = f"{event.timestamp.isoformat()}_{event.event_type.value}_{event.old_count}_{event.new_count}"
            self.collection.insert_one(doc)
            logger.debug(f"Created scaling event: {event.event_type}")
            return True
        except DuplicateKeyError:
            # Event already exists, that's okay
            return True
        except Exception as e:
            logger.error(f"Failed to create scaling event: {e}")
            return False

    def get_recent(self, limit: int = 100, event_type: Optional[ScalingEventType] = None) -> List[ScalingEvent]:
        """Get recent scaling events"""
        query = {}
        if event_type:
            query["event_type"] = event_type.value

        docs = self.collection.find(query).sort("timestamp", DESCENDING).limit(limit)
        return [ScalingEvent.from_dict(doc) for doc in docs]

    def get_events_in_range(self, start: datetime, end: datetime) -> List[ScalingEvent]:
        """Get events within a time range"""
        docs = self.collection.find({
            "timestamp": {"$gte": start, "$lte": end}
        }).sort("timestamp", ASCENDING)
        return [ScalingEvent.from_dict(doc) for doc in docs]

    def get_last_scale_up(self) -> Optional[datetime]:
        """Get timestamp of last scale-up event"""
        doc = self.collection.find_one(
            {"event_type": ScalingEventType.SCALE_UP.value},
            sort=[("timestamp", DESCENDING)]
        )
        return doc["timestamp"] if doc else None

    def get_last_scale_down(self) -> Optional[datetime]:
        """Get timestamp of last scale-down event"""
        doc = self.collection.find_one(
            {"event_type": ScalingEventType.SCALE_DOWN.value},
            sort=[("timestamp", DESCENDING)]
        )
        return doc["timestamp"] if doc else None

    def cleanup_old_events(self, days: int = 30) -> int:
        """Clean up old scaling events"""
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        result = self.collection.delete_many({"timestamp": {"$lt": cutoff_date}})
        return result.deleted_count

    def get_event_counts(self, hours: int = 24) -> Dict[str, int]:
        """Get event counts in the last N hours"""
        start = datetime.utcnow() - timedelta(hours=hours)
        pipeline = [
            {"$match": {"timestamp": {"$gte": start}}},
            {"$group": {"_id": "$event_type", "count": {"$sum": 1}}}
        ]
        return {doc["_id"]: doc["count"] for doc in self.collection.aggregate(pipeline)}


class ClusterStateRepository(MongoDBRepository):
    """Repository for cluster state operations"""

    @property
    def collection(self) -> Collection:
        return self.db.cluster_state

    def set(self, key: str, value: Any, value_type: str = "string", expires_in_seconds: Optional[int] = None) -> bool:
        """Set a cluster state value"""
        try:
            expires_at = None
            if expires_in_seconds:
                expires_at = datetime.utcnow() + timedelta(seconds=expires_in_seconds)

            # Convert value to string if it's complex
            if value_type == "json":
                value = json.dumps(value) if not isinstance(value, str) else value
            elif value_type == "bool":
                value = str(value).lower()
            elif value_type not in ["string", "int", "float"]:
                value = str(value)

            doc = ClusterState(
                key=key,
                value=value,
                value_type=value_type,
                expires_at=expires_at,
                updated_at=datetime.utcnow()
            )

            self.collection.replace_one(
                {"_id": key},
                doc.to_dict(),
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set cluster state {key}: {e}")
            return False

    def get(self, key: str, default: Any = None) -> Any:
        """Get a cluster state value"""
        doc = self.collection.find_one({"_id": key})
        if doc:
            state = ClusterState.from_dict(doc)
            # Handle TTL expiration
            if state.expires_at and state.expires_at < datetime.utcnow():
                self.delete(key)
                return default
            return state.value
        return default

    def delete(self, key: str) -> bool:
        """Delete a cluster state value"""
        try:
            result = self.collection.delete_one({"_id": key})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete cluster state {key}: {e}")
            return False

    def get_all(self) -> Dict[str, Any]:
        """Get all cluster state values"""
        docs = self.collection.find({})
        result = {}
        for doc in docs:
            state = ClusterState.from_dict(doc)
            if not state.expires_at or state.expires_at >= datetime.utcnow():
                result[state.key] = state.value
        return result

    def increment(self, key: str, amount: int = 1) -> int:
        """Increment a counter value"""
        current = self.get(key, 0)
        try:
            new_value = int(current) + amount
        except (ValueError, TypeError):
            new_value = amount
        self.set(key, new_value, "int")
        return new_value


class ScalingRuleRepository(MongoDBRepository):
    """Repository for scaling rule operations"""

    @property
    def collection(self) -> Collection:
        return self.db.scaling_rules

    def save(self, rule: ScalingRule) -> bool:
        """Save or update a scaling rule"""
        try:
            rule.updated_at = datetime.utcnow()
            self.collection.replace_one(
                {"_id": rule.name},
                rule.to_dict(),
                upsert=True
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save scaling rule {rule.name}: {e}")
            return False

    def get(self, name: str) -> Optional[ScalingRule]:
        """Get a scaling rule by name"""
        doc = self.collection.find_one({"_id": name})
        if doc:
            return ScalingRule.from_dict(doc)
        return None

    def get_all(self, enabled_only: bool = True) -> List[ScalingRule]:
        """Get all scaling rules"""
        query = {}
        if enabled_only:
            query["enabled"] = True

        docs = self.collection.find(query).sort("name", ASCENDING)
        return [ScalingRule.from_dict(doc) for doc in docs]

    def delete(self, name: str) -> bool:
        """Delete a scaling rule"""
        try:
            result = self.collection.delete_one({"_id": name})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Failed to delete scaling rule {name}: {e}")
            return False

    def get_active_rules(self) -> List[ScalingRule]:
        """Get all active (enabled and not expired) rules"""
        docs = self.collection.find({
            "enabled": True,
            "$or": [
                {"expires_at": {"$exists": False}},
                {"expires_at": {"$gt": datetime.utcnow()}}
            ]
        }).sort("priority", ASCENDING)
        return [ScalingRule.from_dict(doc) for doc in docs]