# Integration Guide: Race-Free Scaling Patterns

## Overview: Before vs After

### Current Issues in Your Code

```python
# ❌ PROBLEM 1: Race condition in counter
# File: async_scaling.py
worker_num = self.redis_client.increment_worker_counter()  # Good
# ... worker creation fails ...
self.redis_client.decrement_worker_counter()  # BAD - race condition!

# ❌ PROBLEM 2: No locking between check and action
current_nodes = self.database.get_worker_count()
if current_nodes < max_nodes:
    # Another process could scale here!
    self._scale_up(count=1)

# ❌ PROBLEM 3: Inconsistent state after partial failure
container = create_container()  # Succeeds
verify_k8s(container)  # Fails
# Counter not rolled back, state inconsistent
```

### Recommended Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    CLIENT REQUEST                            │
│                  "Scale up by 2 nodes"                       │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│               DISTRIBUTED LOCK (30s TTL)                     │
│           Prevents concurrent scaling operations             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  WRITE-AHEAD LOG (WAL)                       │
│      Log operation BEFORE execution (crash recovery)         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    STATE MACHINE                             │
│  RESERVED → CREATING → VERIFYING → READY                    │
│  (Atomic CAS transitions prevent inconsistency)              │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                 ATOMIC OPERATIONS                            │
│  Redis INCR (counter) + Docker + Kubernetes                  │
│  On failure: Rollback or mark FAILED                         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                RELEASE LOCK + COMPLETE WAL                   │
└─────────────────────────────────────────────────────────────┘
```

---

## Pattern 1: State Machine (Highest Priority)

### Why You Need This
Your current code has ambiguous states. A worker can be:
- In Redis but not Docker
- In Docker but not Kubernetes
- In Kubernetes but marked as "initializing" in Redis

**State machine enforces valid transitions:**

### Implementation in Your Code

**File: `database/mongodb.py`** (Update)

```python
class NodeStatus(str, Enum):
    """Worker lifecycle states with enforced transitions"""
    # Creation flow
    RESERVED = "reserved"        # Number allocated
    CREATING = "creating"        # Docker starting
    VERIFYING = "verifying"      # Waiting for K8s
    READY = "ready"              # Operational
    
    # Removal flow  
    DRAINING = "draining"        # K8s draining
    REMOVING = "removing"        # Container removing
    REMOVED = "removed"          # Done
    
    # Error states
    FAILED = "failed"
    ROLLBACK = "rollback"

# Add valid transitions
VALID_TRANSITIONS = {
    NodeStatus.RESERVED: [NodeStatus.CREATING, NodeStatus.FAILED],
    NodeStatus.CREATING: [NodeStatus.VERIFYING, NodeStatus.FAILED],
    NodeStatus.VERIFYING: [NodeStatus.READY, NodeStatus.FAILED],
    NodeStatus.READY: [NodeStatus.DRAINING, NodeStatus.FAILED],
    NodeStatus.DRAINING: [NodeStatus.REMOVING, NodeStatus.FAILED],
    NodeStatus.REMOVING: [NodeStatus.REMOVED, NodeStatus.FAILED],
}
```

**File: `database/redis_client.py`** (Add method)

```python
def transition_worker_state_cas(
    self, 
    worker_name: str, 
    from_state: str, 
    to_state: str
) -> bool:
    """
    Atomically transition worker state using Lua script (CAS)
    Returns True if successful, False if state changed
    """
    lua_script = """
    local key = KEYS[1]
    local expected = ARGV[1]
    local new_state = ARGV[2]
    local timestamp = ARGV[3]
    
    local current = redis.call('HGET', key, 'status')
    if current == expected then
        redis.call('HSET', key, 'status', new_state)
        redis.call('HSET', key, 'updated_at', timestamp)
        return 1
    else
        return 0
    end
    """
    
    result = self.client.eval(
        lua_script,
        1,
        f"autoscaler:worker:{worker_name}",
        from_state,
        to_state,
        datetime.now(timezone.utc).isoformat()
    )
    
    return result == 1
```

**File: `core/async_scaling.py`** (Update `_create_worker_node_sync`)

```python
def _create_worker_node_sync(self, worker_id: int, docker_client) -> Optional[WorkerNode]:
    """Create worker with state machine transitions"""
    
    # 1. RESERVE NUMBER
    worker_num = self.redis_client.increment_worker_counter()
    node_name = f"{settings.autoscaler.worker_prefix}-{worker_num}"
    
    # 2. SET RESERVED STATE
    worker_node = WorkerNode(
        node_name=node_name,
        container_id="",  # Not yet created
        container_name=node_name,
        status=NodeStatus.RESERVED,
        launched_at=datetime.now(timezone.utc)
    )
    
    # Store in Redis with RESERVED state
    self.redis_client.hset(
        f"worker:{node_name}",
        mapping={
            "status": NodeStatus.RESERVED.value,
            "worker_number": worker_num,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    )
    
    try:
        # 3. TRANSITION TO CREATING (CAS)
        if not self.redis_client.transition_worker_state_cas(
            node_name, 
            NodeStatus.RESERVED.value,
            NodeStatus.CREATING.value
        ):
            # State changed by another process - abort
            self._rollback_worker_number(worker_num, "CAS failed on CREATING transition")
            return None
        
        # 4. CREATE CONTAINER
        container = docker_client.containers.run(
            name=node_name,
            # ... your existing config ...
        )
        
        worker_node.container_id = container.id
        
        # 5. TRANSITION TO VERIFYING
        if not self.redis_client.transition_worker_state_cas(
            node_name,
            NodeStatus.CREATING.value,
            NodeStatus.VERIFYING.value
        ):
            # Cleanup container but don't rollback number
            container.remove(force=True)
            return None
        
        return worker_node
        
    except Exception as e:
        logger.error(f"Failed to create worker: {e}")
        # Only rollback if container never created
        self._rollback_worker_number(worker_num, str(e))
        self.redis_client.hset(
            f"worker:{node_name}",
            "status",
            NodeStatus.FAILED.value
        )
        return None
```

---

## Pattern 2: Distributed Lock (Medium Priority)

### Current Problem
```python
# Two autoscaler instances can execute simultaneously
instance_1: metrics = collect(); should_scale = True; scale_up()
instance_2: metrics = collect(); should_scale = True; scale_up()
# Result: Double scaling!
```

### Solution: Lock Around Scaling Operations

**File: `core/autoscaler.py`** (Update `_execute_scaling`)

```python
async def _execute_scaling(self, decision: Dict[str, Any], metrics: Dict[str, Any] = None) -> bool:
    """Execute scaling with distributed lock"""
    
    lock_key = REDIS_KEYS['SCALING_LOCK']
    lock_id = str(uuid.uuid4())
    
    # Try to acquire lock with 120s TTL
    acquired = self.database.redis.set(
        lock_key,
        lock_id,
        nx=True,  # Only set if not exists
        ex=120    # 2 minute expiration
    )
    
    if not acquired:
        # Check if lock is stale (older than 2 minutes)
        lock_data = self.database.redis.get(lock_key, deserialize=False)
        if lock_data:
            try:
                lock_info = json.loads(lock_data)
                lock_time = datetime.fromisoformat(lock_info.get('timestamp', ''))
                age = (datetime.now(timezone.utc) - lock_time).total_seconds()
                
                if age > 120:
                    # Stale lock - force release
                    logger.warning("Stale lock detected, forcing release")
                    self.database.redis.delete(lock_key)
                    # Retry acquisition
                    acquired = self.database.redis.set(lock_key, lock_id, nx=True, ex=120)
            except:
                pass
        
        if not acquired:
            logger.info("Another scaling operation in progress, skipping")
            return False
    
    try:
        # Execute scaling operation
        action = decision['action']
        count = decision.get('count', 1)
        
        if action == "scale_up":
            return self._scale_up(count, decision)
        elif action == "scale_down":
            return self._scale_down(count, decision, metrics)
        else:
            logger.warning(f"Unknown action: {action}")
            return False
            
    finally:
        # ALWAYS release lock (only if we own it)
        self._release_lock_if_owner(lock_key, lock_id)
```

**Add helper method:**

```python
def _release_lock_if_owner(self, lock_key: str, lock_id: str):
    """Release lock only if we own it (prevents releasing others' locks)"""
    lua_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        redis.call('DEL', KEYS[1])
        return 1
    else
        return 0
    end
    """
    
    result = self.database.redis.client.eval(lua_script, 1, lock_key, lock_id)
    if result == 1:
        logger.info("Released scaling lock")
    else:
        logger.warning("Lock already released or owned by another process")
```

---

## Pattern 3: Write-Ahead Log (Lower Priority, High Value)

### Why You Need This
If autoscaler crashes mid-scale:
- Worker number reserved but container never created
- Container created but not verified
- Node draining but not removed

**WAL allows recovery on restart.**

### Implementation

**File: `database/redis_client.py`** (Add methods)

```python
def log_scaling_operation(self, operation_type: str, worker_name: str, 
                         worker_number: int) -> str:
    """
    Log operation to WAL before execution
    Returns operation_id for tracking
    """
    operation_id = str(uuid.uuid4())
    
    self.hset(
        f"wal:{operation_id}",
        mapping={
            "operation_id": operation_id,
            "type": operation_type,
            "worker_name": worker_name,
            "worker_number": worker_number,
            "state": "started",
            "started_at": datetime.now(timezone.utc).isoformat()
        }
    )
    self.expire(f"wal:{operation_id}", 3600)  # 1 hour TTL
    
    return operation_id

def complete_wal_operation(self, operation_id: str, success: bool = True):
    """Mark operation as complete"""
    self.hset(
        f"wal:{operation_id}",
        mapping={
            "state": "completed" if success else "failed",
            "completed_at": datetime.now(timezone.utc).isoformat()
        }
    )

def get_incomplete_operations(self) -> List[Dict]:
    """Find operations that didn't complete (for recovery)"""
    operations = []
    for key in self.client.keys("autoscaler:wal:*"):
        data = self.hgetall(key[len("autoscaler:"):])
        if "completed_at" not in data:
            operations.append(data)
    return operations
```

**File: `core/autoscaler.py`** (Add recovery on startup)

```python
def __init__(self, config: Dict, database: DatabaseManager):
    # ... existing init ...
    
    # Recover from incomplete operations
    self._recover_from_crash()

def _recover_from_crash(self):
    """Recover from incomplete operations after restart"""
    incomplete = self.database.redis.get_incomplete_operations()
    
    if not incomplete:
        logger.info("No incomplete operations to recover")
        return
    
    logger.warning(f"Found {len(incomplete)} incomplete operations")
    
    for op in incomplete:
        worker_name = op['worker_name']
        op_type = op['type']
        state = op.get('state', 'unknown')
        
        if op_type == "scale_up":
            if state in ["started", "creating"]:
                # Early failure - rollback
                logger.info(f"Rolling back incomplete scale-up: {worker_name}")
                self._rollback_incomplete_scale_up(worker_name, op)
            elif state == "verifying":
                # Check if it eventually succeeded
                if self._check_worker_ready(worker_name):
                    logger.info(f"Worker eventually became ready: {worker_name}")
                    self.database.redis.complete_wal_operation(op['operation_id'], True)
                else:
                    logger.warning(f"Worker never became ready: {worker_name}")
                    self._rollback_incomplete_scale_up(worker_name, op)
        
        elif op_type == "scale_down":
            # Complete the removal
            logger.info(f"Completing interrupted scale-down: {worker_name}")
            self._complete_removal(worker_name)
```

---

## Pattern 4: Counter Monotonicity (Critical!)

### The Golden Rule

```python
# ✅ ONLY TWO OPERATIONS ALLOWED:

# 1. INCREMENT (always safe)
worker_num = redis.increment_worker_counter()

# 2. DECREMENT (ONLY for immediate rollback)
# Conditions that MUST be true:
# - Container was NEVER created
# - Same thread/process that incremented
# - Within same transaction
if container_was_never_created and same_transaction:
    redis.decrement_worker_counter()
else:
    # Number is RETIRED, never reused
    logger.info(f"Worker number {worker_num} retired")
```

### Update Your Code

**File: `core/async_scaling.py`** (Fix `scale_down_concurrent`)

```python
async def scale_down_concurrent(self, workers_to_remove, docker_client, database):
    """Remove workers - NEVER decrement counter"""
    
    # ... your existing removal code ...
    
    for worker, result in zip(db_updates, db_results):
        if isinstance(result, Exception):
            error_messages.append(f"DB error: {worker.node_name}")
        else:
            # ✅ CORRECT: Just log removal
            worker_num = self._extract_worker_number(worker.node_name)
            logger.info(
                f"Removed worker {worker.node_name}. "
                f"Number {worker_num} is retired (counter unchanged)."
            )
            
            # ❌ REMOVE THIS:
            # self.redis_client.decrement_worker_counter()
    
    return successful_removals, error_messages
```

---

## Testing Your Implementation

### Test 1: Concurrent Scale Operations

```python
import asyncio
import concurrent.futures

async def test_concurrent_scaling():
    """Test that only one scale operation runs at a time"""
    
    # Start 3 scale-up operations simultaneously
    tasks = [
        autoscaler.scale_up(count=1),
        autoscaler.scale_up(count=1),
        autoscaler.scale_up(count=1),
    ]
    
    results = await asyncio.gather(*tasks)
    
    # Verify:
    # 1. Only one operation succeeded (lock worked)
    successes = sum(1 for r in results if r[0])
    assert successes == 1, f"Expected 1 success, got {successes}"
    
    # 2. No duplicate worker numbers
    all_workers = [w for r in results for w in r[0]]
    assert len(all_workers) == len(set(all_workers)), "Duplicate workers!"
    
    print("✓ Concurrent operations properly serialized")
```

### Test 2: Crash Recovery

```python
def test_crash_recovery():
    """Simulate crash during scaling"""
    
    # Start scale-up
    op_id = database.redis.log_scaling_operation("scale_up", "k3s-worker-10", 10)
    
    # Simulate crash (kill autoscaler)
    # ...
    
    # Restart autoscaler
    autoscaler = K3sAutoscaler(config, database)
    
    # Should detect incomplete operation
    incomplete = database.redis.get_incomplete_operations()
    assert len(incomplete) > 0, "Didn't detect incomplete operation"
    
    # Should rollback or complete
    autoscaler._recover_from_crash()
    
    # Verify state is consistent
    counter = database.redis.get_next_worker_number()
    workers = database.get_all_workers()
    max_worker = max(int(w.node_name.split('-')[-1]) for w in workers)
    
    assert counter > max_worker, "Counter not ahead of workers"
    print("✓ Crash recovery successful")
```

### Test 3: Counter Monotonicity

```python
def test_counter_never_decreases():
    """Ensure counter only increases"""
    
    initial_counter = database.redis.get_next_worker_number()
    
    # Scale up
    autoscaler.scale_up(count=2)
    after_up = database.redis.get_next_worker_number()
    assert after_up > initial_counter
    
    # Scale down
    workers = database.get_all_workers()
    autoscaler.scale_down([workers[-1].node_name])
    after_down = database.redis.get_next_worker_number()
    
    # Counter should NOT decrease
    assert after_down == after_up, f"Counter decreased! {after_up} → {after_down}"
    
    print("✓ Counter monotonicity preserved")
```

---

## Migration Path (Recommended Order)

### Phase 1: Critical Fixes (Do First)
1. ✅ Fix counter decrement in scale-down
2. ✅ Add distributed lock around scaling operations  
3. ✅ Protect permanent workers in reconciliation

### Phase 2: State Machine (High Value)
4. ✅ Add state transitions (RESERVED → CREATING → VERIFYING → READY)
5. ✅ Implement CAS for atomic state changes
6. ✅ Update async_scaling to use state machine

### Phase 3: Observability (Medium Value)
7. ✅ Add Write-Ahead Log for operations
8. ✅ Implement crash recovery on startup
9. ✅ Add Prometheus metrics for lock contention

### Phase 4: Advanced (Optional)
10. ⚠️ Add lock heartbeat extension for long operations
11. ⚠️ Implement operation retries with exponential backoff
12. ⚠️ Add distributed tracing (OpenTelemetry)

---

## Summary: Key Principles

| Principle | Current | Fixed |
|-----------|---------|-------|
| **Counter** | Decrement on scale-down | NEVER decrement (except rollback) |
| **Locking** | Check in DB, no lock | Distributed lock with TTL |
| **States** | Ambiguous (initializing) | State machine (RESERVED → CREATING → ...) |
| **Atomicity** | Partial rollback | CAS transitions + WAL |
| **Recovery** | Manual cleanup | Automatic crash recovery |

### The Most Important Fix

**File: `async_scaling.py`, line ~235**

```python
# ❌ DELETE THIS ENTIRE BLOCK:
if worker_num:
    new_counter = self.redis_client.decrement_worker_counter()
    logger.info(f"Decremented Redis counter: {new_counter}")

# ✅ REPLACE WITH:
if worker_num:
    logger.info(f"Worker {worker.node_name} removed. Number {worker_num} is retired.")
    # Counter NEVER decrements - gaps in numbering are normal and expected
```

This single change will eliminate your most critical race condition.
