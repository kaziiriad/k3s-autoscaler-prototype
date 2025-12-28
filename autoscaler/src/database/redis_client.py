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

    def scard(self, key: str) -> int:
        """Get size of set (number of members)"""
        try:
            redis_key = self._make_key(key)
            return self.client.scard(redis_key)
        except Exception as e:
            logger.error(f"Failed to get set size for {key}: {e}")
            return 0

    def smembers(self, key: str) -> set:
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

    # ========================================================================
    # UUID-BASED DISTRIBUTED LOCK (Production-grade)
    # ========================================================================

    def acquire_scaling_lock(self, ttl: int = 120) -> Optional[str]:
        """
        Acquire scaling lock with unique ID.

        Args:
            ttl: Lock time-to-live in seconds (auto-releases if crashed)

        Returns:
            Lock ID if acquired, None if lock already held
        """
        import uuid
        lock_key = self._make_key("scaling:operation_in_progress")
        lock_id = str(uuid.uuid4())

        # Use SET NX EX for atomic lock acquisition
        acquired = self.client.set(lock_key, lock_id, nx=True, ex=ttl)

        if acquired:
            logger.info(f"Acquired scaling lock: {lock_id[:8]}...")
            return lock_id
        else:
            # Get existing lock info for debugging
            existing = self.client.get(lock_key)
            logger.debug(f"Scaling lock already held: {existing[:20] if existing else 'unknown'}...")
            return None

    def release_scaling_lock(self, lock_id: str) -> bool:
        """
        Release scaling lock only if we own it (Lua script).

        Args:
            lock_id: The lock ID we received when acquiring

        Returns:
            True if released, False if lock changed or missing
        """
        lock_key = self._make_key("scaling:operation_in_progress")

        # Lua script for safe release (only release our lock)
        lua_script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            redis.call("DEL", KEYS[1])
            return 1
        else
            return 0
        end
        """

        try:
            result = self.client.eval(lua_script, 1, lock_key, lock_id)
            if result == 1:
                logger.info(f"Released scaling lock: {lock_id[:8]}...")
                return True
            else:
                logger.warning(f"Lock value changed or missing: {lock_id[:8]}...")
                return False
        except Exception as e:
            logger.error(f"Failed to release scaling lock: {e}")
            # Force delete as last resort
            try:
                self.client.delete(lock_key)
                logger.warning("Force-deleted scaling lock after release error")
                return True
            except Exception as e2:
                logger.error(f"Failed to force-delete lock: {e2}")
                return False

    # ========================================================================
    # COMPARE-AND-SWAP (CAS) for atomic state transitions
    # ========================================================================

    def cas_transition(self, worker_name: str, from_state: str, to_state: str,
                       metadata: Optional[Dict] = None) -> bool:
        """
        Compare-and-Swap: Atomically transition worker state if current state matches.

        This prevents race conditions where multiple processes try to update
        the same worker's state simultaneously.

        Args:
            worker_name: Name of the worker (e.g., "k3s-worker-1")
            from_state: Expected current state
            to_state: New state to transition to
            metadata: Optional metadata to include in the transition

        Returns:
            True if transition succeeded, False if state didn't match
        """
        worker_key = self._make_key(f"workers:{worker_name}")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Lua script for atomic CAS operation
        lua_script = """
        local key = KEYS[1]
        local expected_state = ARGV[1]
        local new_state = ARGV[2]
        local timestamp = ARGV[3]

        local current_state = redis.call("HGET", key, "state")
        if current_state == expected_state then
            redis.call("HSET", key, "state", new_state)
            redis.call("HSET", key, "state_updated_at", timestamp)
            return 1
        else
            return 0
        end
        """

        try:
            result = self.client.eval(
                lua_script,
                1,  # Number of keys
                worker_key,
                from_state,
                to_state,
                timestamp
            )

            if result == 1:
                logger.info(f"CAS success: {worker_name} {from_state} → {to_state}")
                return True
            else:
                logger.debug(f"CAS failed: {worker_name} state changed by another process")
                return False

        except Exception as e:
            logger.error(f"CAS transition failed for {worker_name}: {e}")
            return False

    def get_worker_state(self, worker_name: str) -> Optional[str]:
        """Get current worker state"""
        worker_key = self._make_key(f"workers:{worker_name}")
        return self.hget(worker_key, "state")

    def set_worker_state(self, worker_name: str, state: str, metadata: Optional[Dict] = None):
        """Set worker state (use only for initial state, not transitions)"""
        worker_key = self._make_key(f"workers:{worker_name}")
        data = {
            "state": state,
            "state_updated_at": datetime.now(timezone.utc).isoformat()
        }
        if metadata:
            data.update(metadata)
        self.hset(worker_key, mapping=data)

    # ========================================================================
    # WRITE-AHEAD LOG (WAL) for crash recovery
    # ========================================================================

    def wal_begin_operation(self, operation_type: str, worker_name: str,
                           worker_number: int, metadata: Optional[Dict] = None) -> str:
        """
        Begin a scaling operation in the Write-Ahead Log.

        WAL allows recovery from incomplete operations after crashes.
        Operations are logged BEFORE execution, enabling rollback/complete.

        Args:
            operation_type: "scale_up" or "scale_down"
            worker_name: Name of the worker (e.g., "k3s-worker-1")
            worker_number: Reserved worker number
            metadata: Optional additional metadata

        Returns:
            operation_id: Unique ID for tracking this operation
        """
        import uuid

        operation_id = str(uuid.uuid4())
        operation_key = f"scaling:wal:{operation_id}"

        operation_data = {
            "operation_id": operation_id,
            "operation_type": operation_type,
            "worker_name": worker_name,
            "worker_number": str(worker_number),
            "state": "started",
            "started_at": datetime.now(timezone.utc).isoformat()
        }

        if metadata:
            operation_data.update(metadata)

        # Write to WAL (1 hour TTL)
        self.hset(operation_key, mapping=operation_data)
        self.client.expire(self._make_key(operation_key), 3600)

        logger.info(f"WAL: Began {operation_type} operation {operation_id[:8]}... for {worker_name}")
        return operation_id

    def wal_update_state(self, operation_id: str, state: str, error: Optional[str] = None):
        """
        Update operation state in WAL.

        Args:
            operation_id: Operation ID from wal_begin_operation
            state: New state (e.g., "creating", "verifying", "completed", "failed")
            error: Optional error message if operation failed
        """
        operation_key = f"scaling:wal:{operation_id}"

        updates = {
            "state": state,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        if error:
            updates["error"] = error

        if state in ["completed", "failed"]:
            updates["completed_at"] = datetime.now(timezone.utc).isoformat()

        self.hset(operation_key, mapping=updates)
        logger.debug(f"WAL: Updated {operation_id[:8]}... → {state}")

    def wal_complete(self, operation_id: str, success: bool = True):
        """
        Mark operation as complete in WAL.

        Args:
            operation_id: Operation ID from wal_begin_operation
            success: True if operation succeeded, False if failed
        """
        final_state = "completed" if success else "failed"
        self.wal_update_state(operation_id, final_state)
        logger.info(f"WAL: Completed {operation_id[:8]}... (success={success})")

    def wal_get_incomplete(self) -> List[Dict]:
        """
        Get all incomplete operations from WAL (for crash recovery).

        Returns:
            List of incomplete operation dicts
        """
        pattern = "scaling:wal:*"
        operations = []

        try:
            keys = self.get_all_keys(pattern)
            for key in keys:
                # Extract operation ID from key
                if key.startswith("scaling:wal:"):
                    op_id = key.split(":")[-1]
                    data = self.hgetall(key)

                    # Check if incomplete (no completed_at)
                    if data and not data.get("completed_at"):
                        operations.append({
                            "operation_id": op_id,
                            "operation_type": data.get("operation_type"),
                            "worker_name": data.get("worker_name"),
                            "worker_number": int(data.get("worker_number", 0)),
                            "state": data.get("state", "unknown"),
                            "started_at": data.get("started_at"),
                            "error": data.get("error")
                        })
        except Exception as e:
            logger.error(f"Failed to get incomplete WAL operations: {e}")

        return operations

    def wal_delete(self, operation_id: str):
        """Delete operation from WAL (after successful recovery)"""
        operation_key = f"scaling:wal:{operation_id}"
        self.delete(operation_key)
        logger.debug(f"WAL: Deleted {operation_id[:8]}...")

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

    # Worker counter operations - Redis is single source of truth
    def get_next_worker_number(self) -> int:
        """Get the next worker number (without incrementing)"""
        return int(self.get("workers:next_number", default=0))

    def increment_worker_counter(self) -> int:
        """Atomically increment and return the next worker number"""
        return self.increment("workers:next_number", 1)

    def decrement_worker_counter(self) -> int:
        """
        Safely decrement worker counter.
        Only call this when rolling back a failed worker creation.
        Returns the new counter value.
        """
        current = self.get("workers:next_number", deserialize=False, default="0")
        try:
            current_val = int(current)
            if current_val > 1:
                new_val = self.decrement("workers:next_number", 1)
                logger.info(f"Decremented worker counter: {current_val} → {new_val}")
                return new_val
            else:
                logger.warning(f"Worker counter at minimum ({current_val}), not decrementing")
                return current_val
        except ValueError:
            logger.error(f"Invalid worker counter value: {current}")
            return 0

    def set_worker_counter(self, value: int) -> bool:
        """
        Set worker counter to a specific value.
        Only used by reconciliation to fix corruption.
        """
        return self.set("workers:next_number", value, serialize=False)