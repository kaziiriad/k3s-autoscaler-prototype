#!/usr/bin/env python3
"""
Redis client for real-time state management
"""

import redis
import json
import pickle
import logging
from typing import Any, Optional, Union, Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class RedisClient:
    """Redis client wrapper for autoscaler state management"""

    def __init__(self, host: str = "localhost", port: int = 6379,
                 db: int = 0, password: Optional[str] = None,
                 decode_responses: bool = True, key_prefix: str = "autoscaler:"):
        """
        Initialize Redis client

        Args:
            host: Redis host
            port: Redis port
            db: Redis database number
            password: Redis password
            decode_responses: Whether to decode responses
            key_prefix: Prefix for all keys
        """
        self.key_prefix = key_prefix
        self.client = None
        self.connect(host, port, db, password, decode_responses)

    def connect(self, host: str, port: int, db: int, password: Optional[str],
                decode_responses: bool):
        """Connect to Redis"""
        try:
            self.client = redis.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                decode_responses=decode_responses,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True
            )
            # Test connection
            self.client.ping()
            logger.info(f"Connected to Redis at {host}:{port}")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    def _make_key(self, key: str) -> str:
        """Add prefix to key"""
        return f"{self.key_prefix}{key}"

    def set(self, key: str, value: Any, expire: Optional[int] = None,
            serialize: bool = True) -> bool:
        """
        Set a key-value pair

        Args:
            key: Key without prefix
            value: Value to store
            expire: Expiration time in seconds
            serialize: Whether to serialize the value

        Returns:
            True if successful
        """
        try:
            redis_key = self._make_key(key)
            if serialize and not isinstance(value, str):
                value = json.dumps(value, default=str)

            return self.client.set(redis_key, value, ex=expire)
        except Exception as e:
            logger.error(f"Failed to set key {key}: {e}")
            return False

    def get(self, key: str, deserialize: bool = True, default: Any = None) -> Any:
        """
        Get a value by key

        Args:
            key: Key without prefix
            deserialize: Whether to deserialize JSON
            default: Default value if key doesn't exist

        Returns:
            Value or default
        """
        try:
            redis_key = self._make_key(key)
            value = self.client.get(redis_key)

            if value is None:
                return default

            if deserialize:
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return value

            return value
        except Exception as e:
            logger.error(f"Failed to get key {key}: {e}")
            return default

    def delete(self, key: str) -> bool:
        """Delete a key"""
        try:
            redis_key = self._make_key(key)
            return bool(self.client.delete(redis_key))
        except Exception as e:
            logger.error(f"Failed to delete key {key}: {e}")
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists"""
        try:
            redis_key = self._make_key(key)
            return bool(self.client.exists(redis_key))
        except Exception as e:
            logger.error(f"Failed to check key {key}: {e}")
            return False

    def expire(self, key: str, seconds: int) -> bool:
        """Set expiration on existing key"""
        try:
            redis_key = self._make_key(key)
            return bool(self.client.expire(redis_key, seconds))
        except Exception as e:
            logger.error(f"Failed to set expiration on key {key}: {e}")
            return False

    def ttl(self, key: str) -> int:
        """Get time to live for key"""
        try:
            redis_key = self._make_key(key)
            return self.client.ttl(redis_key)
        except Exception as e:
            logger.error(f"Failed to get TTL for key {key}: {e}")
            return -1

    def increment(self, key: str, amount: int = 1) -> int:
        """Increment a counter"""
        try:
            redis_key = self._make_key(key)
            return self.client.incr(redis_key, amount)
        except Exception as e:
            logger.error(f"Failed to increment key {key}: {e}")
            return 0

    def decrement(self, key: str, amount: int = 1) -> int:
        """Decrement a counter"""
        try:
            redis_key = self._make_key(key)
            return self.client.decr(redis_key, amount)
        except Exception as e:
            logger.error(f"Failed to decrement key {key}: {e}")
            return 0

    def hset(self, key: str, field: str, value: Any) -> bool:
        """Set hash field"""
        try:
            redis_key = self._make_key(key)
            if not isinstance(value, str):
                value = json.dumps(value, default=str)
            return bool(self.client.hset(redis_key, field, value))
        except Exception as e:
            logger.error(f"Failed to set hash field {key}.{field}: {e}")
            return False

    def hget(self, key: str, field: str, default: Any = None) -> Any:
        """Get hash field"""
        try:
            redis_key = self._make_key(key)
            value = self.client.hget(redis_key, field)
            if value is None:
                return default
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
        except Exception as e:
            logger.error(f"Failed to get hash field {key}.{field}: {e}")
            return default

    def hgetall(self, key: str) -> Dict[str, Any]:
        """Get all hash fields"""
        try:
            redis_key = self._make_key(key)
            data = self.client.hgetall(redis_key)
            result = {}
            for field, value in data.items():
                try:
                    result[field] = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    result[field] = value
            return result
        except Exception as e:
            logger.error(f"Failed to get hash fields for {key}: {e}")
            return {}

    def hdel(self, key: str, field: str) -> bool:
        """Delete hash field"""
        try:
            redis_key = self._make_key(key)
            return bool(self.client.hdel(redis_key, field))
        except Exception as e:
            logger.error(f"Failed to delete hash field {key}.{field}: {e}")
            return False

    def lpush(self, key: str, *values) -> int:
        """Push values to list"""
        try:
            redis_key = self._make_key(key)
            serialized_values = []
            for value in values:
                if not isinstance(value, str):
                    value = json.dumps(value, default=str)
                serialized_values.append(value)
            return self.client.lpush(redis_key, *serialized_values)
        except Exception as e:
            logger.error(f"Failed to lpush to {key}: {e}")
            return 0

    def rpop(self, key: str) -> Any:
        """Pop from list"""
        try:
            redis_key = self._make_key(key)
            value = self.client.rpop(redis_key)
            if value:
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return value
            return None
        except Exception as e:
            logger.error(f"Failed to rpop from {key}: {e}")
            return None

    def lrange(self, key: str, start: int = 0, end: int = -1) -> List[Any]:
        """Get list range"""
        try:
            redis_key = self._make_key(key)
            values = self.client.lrange(redis_key, start, end)
            result = []
            for value in values:
                try:
                    result.append(json.loads(value))
                except (json.JSONDecodeError, TypeError):
                    result.append(value)
            return result
        except Exception as e:
            logger.error(f"Failed to lrange from {key}: {e}")
            return []

    def ltrim(self, key: str, start: int, end: int) -> bool:
        """Trim list"""
        try:
            redis_key = self._make_key(key)
            return bool(self.client.ltrim(redis_key, start, end))
        except Exception as e:
            logger.error(f"Failed to ltrim {key}: {e}")
            return False

    def set_add(self, key: str, *members) -> int:
        """Add members to set"""
        try:
            redis_key = self._make_key(key)
            serialized_members = []
            for member in members:
                if not isinstance(member, str):
                    member = json.dumps(member, default=str)
                serialized_members.append(member)
            return self.client.sadd(redis_key, *serialized_members)
        except Exception as e:
            logger.error(f"Failed to add to set {key}: {e}")
            return 0

    def set_remove(self, key: str, *members) -> int:
        """Remove members from set"""
        try:
            redis_key = self._make_key(key)
            serialized_members = []
            for member in members:
                if not isinstance(member, str):
                    member = json.dumps(member, default=str)
                serialized_members.append(member)
            return self.client.srem(redis_key, *serialized_members)
        except Exception as e:
            logger.error(f"Failed to remove from set {key}: {e}")
            return 0

    def set_members(self, key: str) -> set:
        """Get all set members"""
        try:
            redis_key = self._make_key(key)
            members = self.client.smembers(redis_key)
            result = set()
            for member in members:
                try:
                    result.add(json.loads(member))
                except (json.JSONDecodeError, TypeError):
                    result.add(member)
            return result
        except Exception as e:
            logger.error(f"Failed to get set members for {key}: {e}")
            return set()

    def is_member(self, key: str, member: Any) -> bool:
        """Check if member is in set"""
        try:
            redis_key = self._make_key(key)
            if not isinstance(member, str):
                member = json.dumps(member, default=str)
            return bool(self.client.sismember(redis_key, member))
        except Exception as e:
            logger.error(f"Failed to check set membership for {key}: {e}")
            return False

    def clear_pattern(self, pattern: str) -> int:
        """Clear keys matching pattern"""
        try:
            pattern = self._make_key(pattern)
            keys = self.client.keys(pattern)
            if keys:
                return self.client.delete(*keys)
            return 0
        except Exception as e:
            logger.error(f"Failed to clear pattern {pattern}: {e}")
            return 0

    def get_all_keys(self, pattern: str = "*") -> List[str]:
        """Get all keys matching pattern"""
        try:
            pattern = self._make_key(pattern)
            keys = self.client.keys(pattern)
            # Remove prefix from returned keys
            return [key[len(self.key_prefix):] for key in keys]
        except Exception as e:
            logger.error(f"Failed to get keys for pattern {pattern}: {e}")
            return []

    def close(self):
        """Close Redis connection"""
        if self.client:
            self.client.close()
            logger.info("Redis connection closed")


class AutoscalerRedisClient(RedisClient):
    """Redis client with autoscaler-specific helpers"""

    def __init__(self, **kwargs):
        super().__init__(key_prefix="autoscaler:", **kwargs)

    # Worker node operations
    def set_worker_status(self, node_name: str, status: str, expire: int = 300):
        """Set worker node status with expiration"""
        self.hset(f"workers:{node_name}", "status", status)
        self.expire(f"workers:{node_name}", expire)

    def get_worker_status(self, node_name: str) -> Optional[str]:
        """Get worker node status"""
        return self.hget(f"workers:{node_name}", "status")

    def set_worker_metadata(self, node_name: str, metadata: Dict):
        """Set worker node metadata"""
        for key, value in metadata.items():
            self.hset(f"workers:{node_name}", f"meta:{key}", value)

    def get_worker_metadata(self, node_name: str) -> Dict:
        """Get worker node metadata"""
        all_data = self.hgetall(f"workers:{node_name}")
        return {k.replace("meta:", ""): v for k, v in all_data.items() if k.startswith("meta:")}

    # Cooldown operations
    def set_cooldown(self, action: str, seconds: int):
        """Set cooldown timer"""
        self.set(f"cooldown:{action}", "active", expire=seconds)
        self.set(f"cooldown:{action}:timestamp", datetime.utcnow().isoformat(), expire=seconds)

    def is_cooldown_active(self, action: str) -> bool:
        """Check if cooldown is active"""
        return self.exists(f"cooldown:{action}")

    def get_cooldown_remaining(self, action: str) -> int:
        """Get remaining cooldown time in seconds"""
        return self.ttl(f"cooldown:{action}")

    # Metrics cache
    def cache_metrics(self, metrics: Dict, expire: int = 10):
        """Cache cluster metrics"""
        self.set("metrics:current", metrics, expire)

    def get_cached_metrics(self) -> Optional[Dict]:
        """Get cached metrics"""
        return self.get("metrics:current")

    # Scaling history
    def add_scaling_decision(self, decision: Dict):
        """Add scaling decision to history"""
        self.lpush("scaling:history", decision)
        # Keep last 100 decisions
        self.ltrim("scaling:history", 0, 99)

    def get_scaling_history(self, limit: int = 50) -> List[Dict]:
        """Get scaling decision history"""
        return self.lrange("scaling:history", 0, limit - 1)

    # Lock operations (for distributed autoscaling)
    def acquire_lock(self, resource: str, expire: int = 30) -> bool:
        """Acquire a distributed lock"""
        lock_key = f"locks:{resource}"
        # SETNX with expiration
        return self.set(lock_key, "locked", expire=expire, serialize=False)

    def release_lock(self, resource: str):
        """Release a distributed lock"""
        self.delete(f"locks:{resource}")

    # Health checks
    def record_health_check(self, node_name: str, check_type: str, status: str,
                            message: str = "", response_time: Optional[float] = None):
        """Record health check result"""
        key = f"health:{node_name}:{check_type}"
        data = {
            "status": status,
            "message": message,
            "response_time": response_time,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.set(key, data, expire=300)  # Expire after 5 minutes

    def get_health_status(self, node_name: str) -> Dict:
        """Get all health checks for a node"""
        pattern = f"health:{node_name}:*"
        keys = self.get_all_keys(pattern)
        health_data = {}
        for key in keys:
            check_type = key.split(":")[-1]
            health_data[check_type] = self.get(f"health:{node_name}:{check_type}")
        return health_data