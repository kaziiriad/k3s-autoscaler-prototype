#!/usr/bin/env python3
"""
Redis-based caching for autoscaler data
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import redis
from database import WorkerNode, NodeStatus
from config.settings import settings

logger = logging.getLogger(__name__)


class RedisCache:
    """Redis cache for autoscaler data persistence"""

    def __init__(self):
        """
        Initialize Redis connection using settings
        """
        self.client = None
        self._connect()

    def _connect(self):
        """Establish Redis connection"""
        try:
            # Build Redis URL using settings for cache database
            redis_url = f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.cache_db}"
            if settings.redis.password:
                redis_url = f"redis://:{settings.redis.password}@{settings.redis.host}:{settings.redis.port}/{settings.redis.cache_db}"

            self.client = redis.from_url(redis_url, decode_responses=True)
            self.client.ping()
            logger.info(f"Connected to Redis cache at {redis_url} (using DB {settings.redis.cache_db})")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.client = None

    def is_connected(self) -> bool:
        """Check if Redis is connected"""
        if not self.client:
            return False
        try:
            self.client.ping()
            return True
        except:
            return False

    # Worker node caching
    def cache_workers(self, workers: List[WorkerNode], ttl: int = 3600):
        """Cache worker nodes in Redis"""
        if not self.is_connected():
            return False

        if not workers:
            # Clear cache if no workers
            self.client.delete("autoscaler:workers")
            logger.info("Cleared workers cache (no workers to cache)")
            return True

        try:
            pipe = self.client.pipeline()

            # Clear existing workers
            self.client.delete("autoscaler:workers")

            # Store each worker
            for worker in workers:
                worker_key = f"autoscaler:worker:{worker.node_name}"
                worker_data = {
                    "node_name": worker.node_name,
                    "container_id": worker.container_id,
                    "container_name": worker.container_name,
                    "status": worker.status.value,
                    "launched_at": worker.launched_at.isoformat(),
                    "last_seen": worker.last_seen.isoformat() if worker.last_seen else "",
                    "metadata": json.dumps(worker.metadata or {})
                }
                pipe.hset(worker_key, mapping=worker_data)
                pipe.expire(worker_key, ttl)

                # Add to workers set
                pipe.sadd("autoscaler:workers", worker.node_name)

            # Set TTL on workers set
            pipe.expire("autoscaler:workers", ttl)

            pipe.execute()
            logger.info(f"Cached {len(workers)} workers in Redis")
            return True

        except Exception as e:
            logger.error(f"Failed to cache workers: {e}")
            return False

    def get_cached_workers(self) -> List[WorkerNode]:
        """Get cached worker nodes from Redis"""
        if not self.is_connected():
            return []

        try:
            worker_names = self.client.smembers("autoscaler:workers")
            workers = []

            for name in worker_names:
                worker_key = f"autoscaler:worker:{name}"
                worker_data = self.client.hgetall(worker_key)

                if worker_data:
                    worker = WorkerNode(
                        node_name=worker_data["node_name"],
                        container_id=worker_data["container_id"],
                        container_name=worker_data["container_name"],
                        status=NodeStatus(worker_data["status"]),
                        launched_at=datetime.fromisoformat(worker_data["launched_at"]),
                        last_seen=datetime.fromisoformat(worker_data["last_seen"]) if worker_data["last_seen"] else None,
                        metadata=json.loads(worker_data["metadata"]) if worker_data["metadata"] else {}
                    )
                    workers.append(worker)

            logger.info(f"Retrieved {len(workers)} workers from Redis cache")
            return workers

        except Exception as e:
            logger.error(f"Failed to get cached workers: {e}")
            return []

    def add_worker(self, worker: WorkerNode):
        """Add a single worker to cache"""
        if not self.is_connected():
            return False

        try:
            worker_key = f"autoscaler:worker:{worker.node_name}"
            worker_data = {
                "node_name": worker.node_name,
                "container_id": worker.container_id,
                "container_name": worker.container_name,
                "status": worker.status.value,
                "launched_at": worker.launched_at.isoformat(),
                "last_seen": worker.last_seen.isoformat() if worker.last_seen else "",
                "metadata": json.dumps(worker.metadata or {})
            }

            pipe = self.client.pipeline()
            pipe.hset(worker_key, mapping=worker_data)
            pipe.sadd("autoscaler:workers", worker.node_name)
            pipe.execute()

            logger.info(f"Added worker {worker.node_name} to Redis cache")
            return True

        except Exception as e:
            logger.error(f"Failed to add worker to cache: {e}")
            return False

    def remove_worker(self, node_name: str):
        """Remove a worker from cache"""
        if not self.is_connected():
            return False

        try:
            pipe = self.client.pipeline()
            pipe.delete(f"autoscaler:worker:{node_name}")
            pipe.srem("autoscaler:workers", node_name)
            pipe.execute()

            logger.info(f"Removed worker {node_name} from Redis cache")
            return True

        except Exception as e:
            logger.error(f"Failed to remove worker from cache: {e}")
            return False

    # Cooldown tracking - uses a dedicated cooldown Redis client
    def _get_cooldown_client(self):
        """Get a Redis client for cooldown operations"""
        try:
            # Build Redis URL for cooldown database
            redis_url = f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.cooldown_db}"
            if settings.redis.password:
                redis_url = f"redis://:{settings.redis.password}@{settings.redis.host}:{settings.redis.port}/{settings.redis.cooldown_db}"

            return redis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            logger.error(f"Failed to create cooldown client: {e}")
            return None

    def set_cooldown(self, action: str, duration_seconds: int):
        """Set cooldown for scaling action"""
        try:
            client = self._get_cooldown_client()
            if not client:
                return False

            cooldown_key = f"autoscaler:cooldown:{action}"
            client.setex(cooldown_key, duration_seconds, "active")
            client.close()
            logger.info(f"Set {action} cooldown for {duration_seconds}s")
            return True

        except Exception as e:
            logger.error(f"Failed to set cooldown: {e}")
            return False

    def is_cooldown_active(self, action: str) -> bool:
        """Check if cooldown is active for an action"""
        try:
            client = self._get_cooldown_client()
            if not client:
                return False

            cooldown_key = f"autoscaler:cooldown:{action}"
            active = client.exists(cooldown_key) > 0
            client.close()
            return active

        except Exception as e:
            logger.error(f"Failed to check cooldown: {e}")
            return False

    def get_cooldown_remaining(self, action: str) -> int:
        """Get remaining cooldown time in seconds"""
        try:
            client = self._get_cooldown_client()
            if not client:
                return 0

            cooldown_key = f"autoscaler:cooldown:{action}"
            ttl = client.ttl(cooldown_key)
            client.close()
            return max(0, ttl)

        except Exception as e:
            logger.error(f"Failed to get cooldown remaining: {e}")
            return 0

    # Scaling events
    def store_scale_event(self, event_type: str, count: int, reason: str, metadata: Dict = None):
        """Store a scaling event in Redis"""
        if not self.is_connected():
            return False

        try:
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_type,
                "count": count,
                "reason": reason,
                "metadata": json.dumps(metadata or {})
            }

            # Add to events list (keep last 100 events)
            events_key = "autoscaler:scale_events"
            pipe = self.client.pipeline()
            pipe.lpush(events_key, json.dumps(event))
            pipe.ltrim(events_key, 0, 99)
            pipe.expire(events_key, 86400)  # Keep for 24 hours
            pipe.execute()

            logger.info(f"Stored scaling event: {event_type} {count} nodes - {reason}")
            return True

        except Exception as e:
            logger.error(f"Failed to store scale event: {e}")
            return False

    def get_recent_events(self, limit: int = 10) -> List[Dict]:
        """Get recent scaling events"""
        if not self.is_connected():
            return []

        try:
            events_key = "autoscaler:scale_events"
            event_strings = self.client.lrange(events_key, 0, limit - 1)

            events = []
            for event_str in event_strings:
                try:
                    event = json.loads(event_str)
                    events.append(event)
                except json.JSONDecodeError:
                    continue

            return events

        except Exception as e:
            logger.error(f"Failed to get scale events: {e}")
            return []

    # Cluster state
    def set_cluster_state(self, key: str, value: Any):
        """Set cluster state value"""
        if not self.is_connected():
            return False

        try:
            state_key = f"autoscaler:state:{key}"
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            self.client.set(state_key, value, ex=3600)  # 1 hour TTL
            return True

        except Exception as e:
            logger.error(f"Failed to set cluster state: {e}")
            return False

    def get_cluster_state(self, key: str) -> Any:
        """Get cluster state value"""
        if not self.is_connected():
            return None

        try:
            state_key = f"autoscaler:state:{key}"
            value = self.client.get(state_key)

            if value:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value

            return None

        except Exception as e:
            logger.error(f"Failed to get cluster state: {e}")
            return None

    # Metrics cache
    def cache_metrics(self, metrics: Dict, ttl: int = 10):
        """Cache metrics for short period"""
        if not self.is_connected():
            return False

        try:
            metrics_key = "autoscaler:metrics:current"
            self.client.setex(metrics_key, ttl, json.dumps(metrics))
            return True

        except Exception as e:
            logger.error(f"Failed to cache metrics: {e}")
            return False

    def get_cached_metrics(self) -> Optional[Dict]:
        """Get cached metrics"""
        if not self.is_connected():
            return None

        try:
            metrics_key = "autoscaler:metrics:current"
            metrics_str = self.client.get(metrics_key)

            if metrics_str:
                return json.loads(metrics_str)

            return None

        except Exception as e:
            logger.error(f"Failed to get cached metrics: {e}")
            return None

    def invalidate_cache(self, pattern: str = "autoscaler:*"):
        """Invalidate cache entries matching pattern"""
        if not self.is_connected():
            return False

        try:
            keys = self.client.keys(pattern)
            if keys:
                self.client.delete(*keys)
                logger.info(f"Invalidated {len(keys)} cache entries matching {pattern}")
            return True

        except Exception as e:
            logger.error(f"Failed to invalidate cache: {e}")
            return False