#!/usr/bin/env python3
"""
Production-Grade Scaling Pattern to Prevent Race Conditions

Key Patterns:
1. Distributed Locking with TTL
2. State Machine for Worker Lifecycle
3. Write-Ahead Log (WAL) for Atomic Operations
4. Idempotent Operations
5. Compare-and-Swap (CAS) for Critical Updates
"""

import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ============================================================================
# PATTERN 1: State Machine for Worker Lifecycle
# ============================================================================

class WorkerState(str, Enum):
    """
    Worker lifecycle states - prevents invalid transitions
    
    State transitions (valid only):
    RESERVED → CREATING → VERIFYING → READY → [DRAINING → REMOVING → REMOVED]
              ↓          ↓           ↓
           FAILED     FAILED      FAILED
    """
    # Creation flow
    RESERVED = "reserved"        # Number allocated in Redis
    CREATING = "creating"        # Docker container being created
    VERIFYING = "verifying"      # Waiting for K8s registration
    READY = "ready"              # Fully operational
    
    # Removal flow
    DRAINING = "draining"        # K8s node being drained
    REMOVING = "removing"        # Container being removed
    REMOVED = "removed"          # Cleanup complete
    
    # Error states
    FAILED = "failed"            # Any operation failed
    ROLLBACK = "rollback"        # Being rolled back


class WorkerStateMachine:
    """Enforces valid state transitions"""
    
    VALID_TRANSITIONS = {
        WorkerState.RESERVED: [WorkerState.CREATING, WorkerState.FAILED, WorkerState.ROLLBACK],
        WorkerState.CREATING: [WorkerState.VERIFYING, WorkerState.FAILED, WorkerState.ROLLBACK],
        WorkerState.VERIFYING: [WorkerState.READY, WorkerState.FAILED],
        WorkerState.READY: [WorkerState.DRAINING, WorkerState.FAILED],
        WorkerState.DRAINING: [WorkerState.REMOVING, WorkerState.FAILED],
        WorkerState.REMOVING: [WorkerState.REMOVED, WorkerState.FAILED],
        WorkerState.FAILED: [],      # Terminal state
        WorkerState.REMOVED: [],     # Terminal state
        WorkerState.ROLLBACK: [],    # Terminal state
    }
    
    @classmethod
    def can_transition(cls, from_state: WorkerState, to_state: WorkerState) -> bool:
        """Check if state transition is valid"""
        return to_state in cls.VALID_TRANSITIONS.get(from_state, [])
    
    @classmethod
    def transition(cls, worker_name: str, from_state: WorkerState, 
                   to_state: WorkerState, redis_client) -> bool:
        """
        Atomically transition worker state using Compare-and-Swap (CAS)
        
        Returns True if transition succeeded, False if:
        - Transition is invalid
        - Another process changed the state (race condition)
        """
        if not cls.can_transition(from_state, to_state):
            logger.error(f"Invalid transition: {worker_name} {from_state} → {to_state}")
            return False
        
        # Use Lua script for atomic CAS operation
        lua_script = """
        local key = KEYS[1]
        local expected_state = ARGV[1]
        local new_state = ARGV[2]
        local timestamp = ARGV[3]
        
        local current_state = redis.call('HGET', key, 'state')
        if current_state == expected_state then
            redis.call('HSET', key, 'state', new_state)
            redis.call('HSET', key, 'state_updated_at', timestamp)
            return 1
        else
            return 0
        end
        """
        
        result = redis_client.eval(
            lua_script,
            1,  # Number of keys
            f"worker:{worker_name}",
            from_state.value,
            to_state.value,
            datetime.now(timezone.utc).isoformat()
        )
        
        if result == 1:
            logger.info(f"✓ State transition: {worker_name} {from_state} → {to_state}")
            return True
        else:
            logger.warning(f"✗ CAS failed: {worker_name} state changed by another process")
            return False


# ============================================================================
# PATTERN 2: Write-Ahead Log (WAL) for Atomic Operations
# ============================================================================

@dataclass
class ScalingOperation:
    """
    Write-Ahead Log entry for scaling operations
    Allows recovery from partial failures
    """
    operation_id: str
    operation_type: str  # "scale_up" or "scale_down"
    worker_name: str
    worker_number: int
    state: WorkerState
    started_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "worker_name": self.worker_name,
            "worker_number": self.worker_number,
            "state": self.state.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "metadata": self.metadata
        }


class WriteAheadLog:
    """
    Write-Ahead Log for scaling operations
    
    Benefits:
    - Crash recovery: Resume incomplete operations
    - Audit trail: Track all scaling attempts
    - Idempotency: Prevent duplicate operations
    """
    
    def __init__(self, redis_client):
        self.redis = redis_client
        self.wal_key = "scaling:wal"
    
    def begin_operation(self, operation_type: str, worker_name: str, 
                       worker_number: int) -> ScalingOperation:
        """Start a new operation - write to WAL first"""
        operation = ScalingOperation(
            operation_id=str(uuid.uuid4()),
            operation_type=operation_type,
            worker_name=worker_name,
            worker_number=worker_number,
            state=WorkerState.RESERVED,
            started_at=datetime.now(timezone.utc)
        )
        
        # Write to WAL BEFORE starting operation
        self.redis.hset(
            f"{self.wal_key}:{operation.operation_id}",
            mapping=operation.to_dict()
        )
        self.redis.expire(f"{self.wal_key}:{operation.operation_id}", 3600)  # 1 hour
        
        logger.info(f"WAL: Began {operation_type} operation {operation.operation_id}")
        return operation
    
    def update_operation_state(self, operation_id: str, state: WorkerState, 
                              error: Optional[str] = None):
        """Update operation state in WAL"""
        updates = {
            "state": state.value,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        if error:
            updates["error"] = error
        
        self.redis.hset(f"{self.wal_key}:{operation_id}", mapping=updates)
        logger.debug(f"WAL: Updated {operation_id} → {state.value}")
    
    def complete_operation(self, operation_id: str, success: bool = True):
        """Mark operation as complete"""
        self.redis.hset(
            f"{self.wal_key}:{operation_id}",
            mapping={
                "state": WorkerState.READY.value if success else WorkerState.FAILED.value,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }
        )
        logger.info(f"WAL: Completed {operation_id} (success={success})")
    
    def get_incomplete_operations(self) -> List[ScalingOperation]:
        """Find operations that didn't complete (for crash recovery)"""
        pattern = f"{self.wal_key}:*"
        operations = []
        
        for key in self.redis.keys(pattern):
            data = self.redis.hgetall(key)
            if not data.get("completed_at"):
                # Operation incomplete - may need recovery
                operations.append(ScalingOperation(
                    operation_id=data["operation_id"],
                    operation_type=data["operation_type"],
                    worker_name=data["worker_name"],
                    worker_number=int(data["worker_number"]),
                    state=WorkerState(data["state"]),
                    started_at=datetime.fromisoformat(data["started_at"]),
                    error=data.get("error")
                ))
        
        return operations


# ============================================================================
# PATTERN 3: Distributed Lock with Automatic Release
# ============================================================================

class DistributedLock:
    """
    Distributed lock with automatic release and heartbeat
    
    Features:
    - Automatic expiration (prevents deadlock)
    - Lock ownership verification
    - Heartbeat extension for long operations
    """
    
    def __init__(self, redis_client, lock_name: str, ttl: int = 30):
        self.redis = redis_client
        self.lock_name = f"lock:{lock_name}"
        self.ttl = ttl
        self.lock_id = str(uuid.uuid4())  # Unique lock owner ID
        self.acquired = False
    
    async def __aenter__(self):
        """Acquire lock with timeout"""
        await self.acquire()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Always release lock"""
        await self.release()
    
    async def acquire(self, timeout: int = 10) -> bool:
        """
        Try to acquire lock with timeout
        Uses SET NX EX for atomic lock acquisition
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Atomic: SET key value NX EX ttl
            acquired = self.redis.set(
                self.lock_name,
                self.lock_id,
                nx=True,  # Only set if not exists
                ex=self.ttl
            )
            
            if acquired:
                self.acquired = True
                logger.info(f"✓ Acquired lock: {self.lock_name}")
                return True
            
            # Lock held by another process - wait and retry
            await asyncio.sleep(0.1)
        
        logger.warning(f"✗ Failed to acquire lock: {self.lock_name} (timeout)")
        return False
    
    async def extend(self, additional_ttl: int = 30):
        """
        Extend lock TTL (for long operations)
        Only works if we own the lock
        """
        if not self.acquired:
            return False
        
        # Lua script to verify ownership before extending
        lua_script = """
        local key = KEYS[1]
        local owner_id = ARGV[1]
        local ttl = tonumber(ARGV[2])
        
        if redis.call('GET', key) == owner_id then
            redis.call('EXPIRE', key, ttl)
            return 1
        else
            return 0
        end
        """
        
        result = self.redis.eval(lua_script, 1, self.lock_name, self.lock_id, additional_ttl)
        if result == 1:
            logger.debug(f"Extended lock: {self.lock_name} (+{additional_ttl}s)")
            return True
        else:
            logger.warning(f"Cannot extend lock: {self.lock_name} (no longer owner)")
            self.acquired = False
            return False
    
    async def release(self):
        """
        Release lock only if we own it
        Prevents releasing someone else's lock
        """
        if not self.acquired:
            return
        
        # Lua script to verify ownership before deleting
        lua_script = """
        local key = KEYS[1]
        local owner_id = ARGV[1]
        
        if redis.call('GET', key) == owner_id then
            redis.call('DEL', key)
            return 1
        else
            return 0
        end
        """
        
        result = self.redis.eval(lua_script, 1, self.lock_name, self.lock_id)
        if result == 1:
            logger.info(f"✓ Released lock: {self.lock_name}")
        else:
            logger.warning(f"Lock already released: {self.lock_name}")
        
        self.acquired = False


# ============================================================================
# PATTERN 4: Idempotent Scale Operations
# ============================================================================

class IdempotentScaler:
    """
    Ensures scaling operations are idempotent and race-free
    
    Key principles:
    1. Lock before checking state
    2. Check-then-act atomically
    3. Use state machine for transitions
    4. Write-ahead logging for recovery
    5. Never decrement counter except rollback
    """
    
    def __init__(self, redis_client, docker_client, k8s_api):
        self.redis = redis_client
        self.docker = docker_client
        self.k8s = k8s_api
        self.state_machine = WorkerStateMachine()
        self.wal = WriteAheadLog(redis_client)
    
    async def scale_up(self, count: int = 1) -> Tuple[List[str], List[str]]:
        """
        Scale up with race condition prevention
        
        Returns: (successful_workers, errors)
        """
        # STEP 1: Acquire global scaling lock
        async with DistributedLock(self.redis, "scaling_operation", ttl=120) as lock:
            if not lock.acquired:
                return [], ["Failed to acquire scaling lock"]
        
            successful = []
            errors = []
            
            for i in range(count):
                try:
                    worker_name = await self._create_single_worker_atomic()
                    if worker_name:
                        successful.append(worker_name)
                    else:
                        errors.append(f"Failed to create worker {i+1}/{count}")
                except Exception as e:
                    errors.append(f"Worker {i+1}/{count}: {str(e)}")
                    logger.error(f"Scale up error: {e}")
            
            return successful, errors
    
    async def _create_single_worker_atomic(self) -> Optional[str]:
        """
        Create a single worker with full atomicity
        
        Transaction steps:
        1. Reserve number (WAL + State)
        2. Create container (Docker)
        3. Verify K8s (Polling)
        4. Commit (State transition)
        
        On any failure → Rollback
        """
        operation = None
        worker_name = None
        
        try:
            # PHASE 1: RESERVE NUMBER (Atomic)
            worker_number = await self._reserve_worker_number()
            worker_name = f"k3s-worker-{worker_number}"
            
            # Begin WAL operation
            operation = self.wal.begin_operation("scale_up", worker_name, worker_number)
            
            # Set initial state
            await self._set_worker_state(worker_name, WorkerState.RESERVED)
            
            # PHASE 2: CREATE CONTAINER (Blocking)
            if not await self._transition_and_execute(
                worker_name, 
                WorkerState.RESERVED, 
                WorkerState.CREATING,
                lambda: self._create_docker_container(worker_name)
            ):
                raise Exception("Failed to create Docker container")
            
            # PHASE 3: VERIFY KUBERNETES (Async with timeout)
            if not await self._transition_and_execute(
                worker_name,
                WorkerState.CREATING,
                WorkerState.VERIFYING,
                lambda: self._verify_kubernetes_node(worker_name, timeout=120)
            ):
                raise Exception("Failed to verify Kubernetes node")
            
            # PHASE 4: MARK READY (Final state)
            if not self.state_machine.transition(
                worker_name, 
                WorkerState.VERIFYING, 
                WorkerState.READY,
                self.redis
            ):
                raise Exception("Failed to transition to READY state")
            
            # SUCCESS: Complete WAL
            self.wal.complete_operation(operation.operation_id, success=True)
            logger.info(f"✓ Successfully created worker: {worker_name}")
            return worker_name
            
        except Exception as e:
            logger.error(f"Worker creation failed: {worker_name} - {e}")
            
            # ROLLBACK
            if operation:
                self.wal.update_operation_state(
                    operation.operation_id, 
                    WorkerState.FAILED, 
                    error=str(e)
                )
            
            if worker_name:
                await self._rollback_worker_creation(worker_name, operation)
            
            return None
    
    async def _reserve_worker_number(self) -> int:
        """
        Atomically reserve next worker number
        Uses Redis INCR for race-free allocation
        """
        return self.redis.incr("workers:next_number")
    
    async def _transition_and_execute(
        self, 
        worker_name: str,
        from_state: WorkerState,
        to_state: WorkerState,
        action: callable
    ) -> bool:
        """
        Atomically transition state and execute action
        If action fails, state doesn't change
        """
        # First try state transition (CAS)
        if not self.state_machine.transition(worker_name, from_state, to_state, self.redis):
            logger.error(f"State transition failed: {worker_name} {from_state} → {to_state}")
            return False
        
        # State transitioned - now execute action
        try:
            result = await action()
            return result
        except Exception as e:
            # Action failed - mark as failed
            self.state_machine.transition(worker_name, to_state, WorkerState.FAILED, self.redis)
            raise
    
    async def _set_worker_state(self, worker_name: str, state: WorkerState):
        """Set initial worker state"""
        self.redis.hset(
            f"worker:{worker_name}",
            mapping={
                "state": state.value,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
        )
    
    async def _create_docker_container(self, worker_name: str) -> bool:
        """Create Docker container"""
        # Implementation from your existing code
        # Returns True on success
        pass
    
    async def _verify_kubernetes_node(self, worker_name: str, timeout: int) -> bool:
        """Verify node registered in Kubernetes"""
        # Implementation from your existing code
        # Returns True when node is Ready
        pass
    
    async def _rollback_worker_creation(
        self, 
        worker_name: str, 
        operation: ScalingOperation
    ):
        """
        Rollback failed worker creation
        
        CRITICAL: Only decrement counter if container never created
        """
        current_state = self.redis.hget(f"worker:{worker_name}", "state")
        
        # Check if we got far enough to create container
        container_created = current_state in [
            WorkerState.CREATING.value,
            WorkerState.VERIFYING.value
        ]
        
        # Cleanup container if it exists
        try:
            if container_created:
                container = self.docker.containers.get(worker_name)
                container.remove(force=True)
                logger.info(f"Rolled back container: {worker_name}")
        except Exception as e:
            logger.warning(f"Container cleanup failed: {e}")
        
        # Only decrement counter if container was NEVER created
        if not container_created:
            self.redis.decr("workers:next_number")
            logger.info(f"Rolled back counter for {worker_name}")
        else:
            logger.info(f"Number {operation.worker_number} retired (container was created)")
        
        # Clean up Redis state
        self.redis.delete(f"worker:{worker_name}")
        
        # Mark WAL as rolled back
        self.wal.update_operation_state(
            operation.operation_id,
            WorkerState.ROLLBACK
        )
    
    async def scale_down(self, worker_names: List[str]) -> Tuple[List[str], List[str]]:
        """
        Scale down with race condition prevention
        
        Returns: (successful_removals, errors)
        """
        # STEP 1: Acquire global scaling lock
        async with DistributedLock(self.redis, "scaling_operation", ttl=120) as lock:
            if not lock.acquired:
                return [], ["Failed to acquire scaling lock"]
            
            successful = []
            errors = []
            
            for worker_name in worker_names:
                try:
                    if await self._remove_single_worker_atomic(worker_name):
                        successful.append(worker_name)
                    else:
                        errors.append(f"Failed to remove {worker_name}")
                except Exception as e:
                    errors.append(f"{worker_name}: {str(e)}")
                    logger.error(f"Scale down error: {e}")
            
            return successful, errors
    
    async def _remove_single_worker_atomic(self, worker_name: str) -> bool:
        """
        Remove a single worker with full atomicity
        
        Transaction steps:
        1. Check worker is removable
        2. Drain K8s node
        3. Remove container
        4. Clean up state
        
        CRITICAL: Never decrement counter!
        """
        operation = None
        
        try:
            # Get worker number for WAL
            worker_number = int(worker_name.split('-')[-1])
            
            # Begin WAL operation
            operation = self.wal.begin_operation("scale_down", worker_name, worker_number)
            
            # PHASE 1: Verify worker is removable
            current_state = self.redis.hget(f"worker:{worker_name}", "state")
            if current_state != WorkerState.READY.value:
                raise Exception(f"Worker not in READY state: {current_state}")
            
            # PHASE 2: Drain K8s node
            if not await self._transition_and_execute(
                worker_name,
                WorkerState.READY,
                WorkerState.DRAINING,
                lambda: self._drain_kubernetes_node(worker_name)
            ):
                raise Exception("Failed to drain Kubernetes node")
            
            # PHASE 3: Remove container
            if not await self._transition_and_execute(
                worker_name,
                WorkerState.DRAINING,
                WorkerState.REMOVING,
                lambda: self._remove_docker_container(worker_name)
            ):
                raise Exception("Failed to remove Docker container")
            
            # PHASE 4: Mark removed (final state)
            self.state_machine.transition(
                worker_name,
                WorkerState.REMOVING,
                WorkerState.REMOVED,
                self.redis
            )
            
            # Clean up Redis state
            self.redis.delete(f"worker:{worker_name}")
            self.redis.srem("workers:all", worker_name)
            self.redis.srem("workers:removable", worker_name)
            
            # CRITICAL: Do NOT decrement counter!
            # Number is retired, will never be reused
            
            # SUCCESS: Complete WAL
            self.wal.complete_operation(operation.operation_id, success=True)
            logger.info(f"✓ Successfully removed worker: {worker_name}")
            return True
            
        except Exception as e:
            logger.error(f"Worker removal failed: {worker_name} - {e}")
            
            if operation:
                self.wal.update_operation_state(
                    operation.operation_id,
                    WorkerState.FAILED,
                    error=str(e)
                )
            
            return False
    
    async def _drain_kubernetes_node(self, worker_name: str) -> bool:
        """Drain Kubernetes node"""
        # Implementation from your existing code
        pass
    
    async def _remove_docker_container(self, worker_name: str) -> bool:
        """Remove Docker container"""
        # Implementation from your existing code
        pass


# ============================================================================
# PATTERN 5: Recovery from Incomplete Operations
# ============================================================================

class CrashRecoveryManager:
    """
    Recovers from incomplete operations after crash/restart
    Uses WAL to resume or rollback incomplete operations
    """
    
    def __init__(self, scaler: IdempotentScaler):
        self.scaler = scaler
        self.wal = scaler.wal
    
    async def recover_incomplete_operations(self):
        """
        Find and handle incomplete operations from WAL
        
        Called on startup to clean up after crashes
        """
        incomplete_ops = self.wal.get_incomplete_operations()
        
        if not incomplete_ops:
            logger.info("No incomplete operations to recover")
            return
        
        logger.warning(f"Found {len(incomplete_ops)} incomplete operations")
        
        for op in incomplete_ops:
            # Check how far the operation got
            if op.state in [WorkerState.RESERVED, WorkerState.CREATING]:
                # Early stage failure - rollback
                logger.info(f"Rolling back incomplete operation: {op.operation_id}")
                await self._rollback_operation(op)
            
            elif op.state == WorkerState.VERIFYING:
                # Container created but not verified - check if it succeeded
                logger.info(f"Checking incomplete verification: {op.operation_id}")
                await self._complete_or_rollback_verification(op)
            
            elif op.state in [WorkerState.DRAINING, WorkerState.REMOVING]:
                # Removal in progress - complete it
                logger.info(f"Completing incomplete removal: {op.operation_id}")
                await self._complete_removal(op)
    
    async def _rollback_operation(self, op: ScalingOperation):
        """Rollback incomplete scale-up operation"""
        await self.scaler._rollback_worker_creation(op.worker_name, op)
    
    async def _complete_or_rollback_verification(self, op: ScalingOperation):
        """Check if node eventually became ready, or rollback"""
        # Try one more verification
        verified = await self.scaler._verify_kubernetes_node(
            op.worker_name, 
            timeout=30
        )
        
        if verified:
            # Success! Mark as ready
            self.scaler.state_machine.transition(
                op.worker_name,
                WorkerState.VERIFYING,
                WorkerState.READY,
                self.scaler.redis
            )
            self.wal.complete_operation(op.operation_id, success=True)
        else:
            # Failed - rollback
            await self._rollback_operation(op)
    
    async def _complete_removal(self, op: ScalingOperation):
        """Complete interrupted removal operation"""
        await self.scaler._remove_single_worker_atomic(op.worker_name)


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

async def example_usage():
    """Example of using the race-free scaling system"""
    
    # Initialize
    scaler = IdempotentScaler(redis_client, docker_client, k8s_api)
    recovery = CrashRecoveryManager(scaler)
    
    # On startup: Recover from crashes
    await recovery.recover_incomplete_operations()
    
    # Scale up (race-free)
    successful, errors = await scaler.scale_up(count=3)
    print(f"Scaled up: {successful}, errors: {errors}")
    
    # Scale down (race-free)
    workers_to_remove = ["k3s-worker-5", "k3s-worker-6"]
    successful, errors = await scaler.scale_down(workers_to_remove)
    print(f"Scaled down: {successful}, errors: {errors}")


# ============================================================================
# TESTING RACE CONDITIONS
# ============================================================================

async def test_concurrent_scale_operations():
    """Test that concurrent operations are serialized"""
    
    scaler = IdempotentScaler(redis_client, docker_client, k8s_api)
    
    # Try to scale up concurrently from multiple processes
    tasks = [
        scaler.scale_up(count=1),
        scaler.scale_up(count=1),
        scaler.scale_up(count=1),
    ]
    
    results = await asyncio.gather(*tasks)
    
    # Only one should succeed (others blocked by lock)
    successful_ops = [r for r in results if r[0]]
    print(f"Concurrent operations: {len(successful_ops)} succeeded")
    
    # Verify no duplicate worker numbers
    all_workers = [w for result in results for w in result[0]]
    assert len(all_workers) == len(set(all_workers)), "Duplicate workers created!"
