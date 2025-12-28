# Worker Number Management Strategy

## Source of Truth Hierarchy

```
┌─────────────────────────────────────────────────────────────┐
│                   SINGLE SOURCE OF TRUTH                     │
│                                                               │
│  Redis: WORKER_COUNTER (atomic, never decreases*)           │
│  * Only decreases on failed worker creation rollback         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   DERIVED STATE                              │
│                                                               │
│  Redis Sets:                                                 │
│  • workers:all         - All worker names                    │
│  • workers:permanent   - Permanent workers (1,2)             │
│  • workers:removable   - Can be scaled down                  │
│                                                               │
│  Redis Hashes:                                               │
│  • worker:{name}       - Individual worker metadata          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   PHYSICAL REALITY                           │
│                                                               │
│  Docker Containers: Actual running workers                   │
│  • Must sync with Redis on reconciliation                    │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   HISTORICAL RECORD                          │
│                                                               │
│  MongoDB: Scaling events (append-only log)                   │
│  • NOT used for current state decisions                      │
└─────────────────────────────────────────────────────────────┘
```

## Core Principles

### 1. **Counter Monotonicity**
```python
# ✅ CORRECT: Counter only increases
worker_num = redis.increment_worker_counter()
# Result: 1, 2, 3, 4, 5, 6, 7, ...

# ❌ WRONG: Never decrement during normal operations
redis.decrement_worker_counter()  # DO NOT DO THIS on scale-down!
```

**Why?**
- Prevents race conditions between concurrent scale operations
- Ensures unique worker numbers even with failures
- Simplifies reconciliation logic

### 2. **Number Gaps Are Acceptable**
```
Active Workers: [1, 2, 5, 8, 9]
Counter Value: 10

Workers 3, 4, 6, 7 were removed - this is FINE!
Next worker will be #10, not #6
```

### 3. **Three-Phase Worker Lifecycle**

#### Phase 1: Number Reservation (Atomic)
```python
worker_num = redis.increment_worker_counter()  # Atomic INCR
node_name = f"k3s-worker-{worker_num}"
# At this point, number is RESERVED
```

#### Phase 2: Container Creation (Blocking)
```python
try:
    container = docker.containers.run(...)
    redis.set_add("workers:all", node_name)
    redis.set_add("workers:removable", node_name)
except Exception as e:
    # ROLLBACK: Only time we decrement
    redis.decrement_worker_counter()
    raise
```

#### Phase 3: Kubernetes Verification (Async)
```python
if await verify_k8s_node(node_name):
    redis.hset(f"worker:{node_name}", "status", "ready")
else:
    # Cleanup but DON'T decrement counter
    cleanup_failed_worker(node_name)
```

## Key Operations

### Scale Up (Correct Implementation)

```python
async def scale_up_worker(self) -> Optional[WorkerNode]:
    """Create new worker with atomic number allocation"""
    
    # 1. Reserve number atomically (source of truth)
    worker_num = self.redis.increment_worker_counter()
    node_name = f"k3s-worker-{worker_num}"
    
    # 2. Create container
    try:
        container = docker_client.containers.run(
            name=node_name,
            image="rancher/k3s:v1.29.1-k3s1",
            # ... other config
        )
    except Exception as e:
        # Rollback: only valid time to decrement
        self.redis.decrement_worker_counter()
        raise WorkerCreationError(f"Failed at container creation: {e}")
    
    # 3. Register in Redis
    self.redis.set_add("workers:all", node_name)
    self.redis.set_add("workers:removable", node_name)
    
    # 4. Verify with Kubernetes (async)
    if not await self.verify_k8s_node(node_name, timeout=120):
        # Cleanup but DON'T touch counter
        await self.cleanup_failed_worker(node_name)
        raise WorkerCreationError(f"Failed K8s verification: {node_name}")
    
    return WorkerNode(node_name=node_name, ...)
```

### Scale Down (Correct Implementation)

```python
async def scale_down_worker(self, node_name: str) -> bool:
    """Remove worker - counter NEVER decrements"""
    
    # 1. Remove from Redis sets
    self.redis.set_remove("workers:all", node_name)
    self.redis.set_remove("workers:removable", node_name)
    
    # 2. Drain Kubernetes node
    await self.drain_k8s_node(node_name)
    
    # 3. Remove Docker container
    container = docker_client.containers.get(node_name)
    container.remove(force=True)
    
    # 4. Clean up Redis hash
    self.redis.delete(f"worker:{node_name}")
    
    # ⚠️ IMPORTANT: DO NOT DECREMENT COUNTER
    # The number is retired, will never be reused
    
    return True
```

### Reconciliation (Fix Drift)

```python
def reconcile_worker_counter(self):
    """Ensure counter is ahead of all existing workers"""
    
    # Get all Docker containers
    containers = docker_client.containers.list()
    worker_containers = [c.name for c in containers 
                        if c.name.startswith("k3s-worker-")]
    
    # Find highest worker number
    max_worker_num = 0
    for name in worker_containers:
        num = int(name.split("-")[-1])
        max_worker_num = max(max_worker_num, num)
    
    # Get current counter
    current_counter = self.redis.get_next_worker_number()
    
    # Counter must be AHEAD of all existing workers
    required_counter = max_worker_num + 1
    
    if current_counter < required_counter:
        # Fix corruption
        self.redis.set_worker_counter(required_counter)
        logger.warning(f"Fixed counter: {current_counter} → {required_counter}")
    elif current_counter > required_counter:
        # This is NORMAL - workers were created and removed
        logger.info(f"Counter ahead of workers (OK): counter={current_counter}, max_worker={max_worker_num}")
```

## State Queries (How to Check Current State)

```python
# ✅ Get worker count (use Redis, not counter!)
worker_count = redis.scard("workers:all")

# ❌ WRONG: Don't use counter for worker count
worker_count = redis.get_next_worker_number() - 1  # INCORRECT!

# ✅ Get removable workers for scale-down
removable = redis.smembers("workers:removable")
removable = removable - permanent_workers

# ✅ Select worker for removal (LIFO)
workers = sorted(removable, key=lambda x: int(x.split("-")[-1]))
worker_to_remove = workers[-1]  # Highest number (most recent)
```

## Error Scenarios and Handling

### Scenario 1: Container Creation Fails
```python
worker_num = redis.increment_worker_counter()  # Counter = 5
try:
    container = create_container(f"k3s-worker-{worker_num}")
except DockerException:
    redis.decrement_worker_counter()  # Counter = 4 (rollback OK)
    # Next attempt will use 5 again
```

### Scenario 2: Kubernetes Verification Fails
```python
worker_num = redis.increment_worker_counter()  # Counter = 5
container = create_container(f"k3s-worker-{worker_num}")  # Success
# Container exists in Docker

if not verify_k8s(node_name):
    cleanup_container(node_name)  # Remove container
    # ⚠️ DON'T decrement counter! Number is burned.
    # Next worker will be #6, not #5
```

**Why?** Container was created and may have left traces in the system. Reusing the number could cause conflicts.

### Scenario 3: Autoscaler Restarts
```python
# On startup reconciliation:
docker_workers = ["k3s-worker-1", "k3s-worker-2", "k3s-worker-5"]
redis_counter = 3  # Stale!

max_num = max([1, 2, 5]) = 5
required_counter = 5 + 1 = 6

redis.set_worker_counter(6)  # Fix
# Next worker will be #6 (correct)
```

## Implementation Checklist

- [x] Redis WORKER_COUNTER is single source of truth
- [x] Counter increments atomically (INCR command)
- [ ] **Fix scale-down to NOT decrement counter** ← Your current issue!
- [x] Counter only decrements on immediate rollback
- [x] Use Redis sets for worker tracking, not counter
- [x] Reconciliation ensures counter ≥ max_worker_num + 1
- [x] LIFO scaling uses sorted worker numbers
- [ ] Remove counter decrement from `_remove_worker_container_sync()`
- [ ] Update documentation to reflect monotonic counter

## Code Changes Needed

### Fix 1: Remove Counter Decrement from Scale-Down

**File: `async_scaling.py`, line ~235**

```python
# ❌ REMOVE THIS BLOCK:
if isinstance(result, Exception):
    error_messages.append(f"DB removal error for {worker.node_name}: {str(result)}")
else:
    # Successfully removed from database, now decrement Redis counter
    worker_num = self._extract_worker_number(worker.node_name)
    if worker_num:
        new_counter = self.redis_client.decrement_worker_counter()  # DELETE THIS
        logger.info(f"Decremented Redis counter after removing {worker.node_name}: {new_counter}")

# ✅ REPLACE WITH:
if isinstance(result, Exception):
    error_messages.append(f"DB removal error for {worker.node_name}: {str(result)}")
else:
    # Worker successfully removed from DB
    # Counter is NOT decremented - number is retired
    worker_num = self._extract_worker_number(worker.node_name)
    logger.info(f"Removed worker {worker.node_name} (number {worker_num} is retired)")
```

### Fix 2: Update Cleanup Logic

**File: `async_scaling.py`, line ~153**

```python
def _cleanup_failed_worker(self, worker: WorkerNode, docker_client, error_msg: str) -> bool:
    """Clean up failed worker - only decrement counter if container creation failed"""
    
    # Check if this is an early failure (container never created)
    container_created = worker.container_id is not None
    
    if container_created:
        # Container was created, don't decrement counter
        logger.warning(f"Container was created for {worker.node_name}, number is retired")
        rollback_counter = False
    else:
        # Early failure, safe to decrement
        rollback_counter = True
    
    # ... existing cleanup code ...
    
    if rollback_counter:
        worker_num = worker.metadata.get("worker_number")
        if worker_num:
            self._rollback_worker_number(worker_num, error_msg)
```

## Summary

**Your source of truth should be:**

1. **Redis WORKER_COUNTER** - Monotonically increasing, atomic
2. **Redis Sets** - Track which workers exist (`workers:all`, `workers:removable`)
3. **Docker** - Physical verification during reconciliation
4. **MongoDB** - Historical record only, NOT for state

**Key Rule:** Counter goes up on scale-up, NEVER down on scale-down (except immediate rollback).

This prevents race conditions, simplifies logic, and makes reconciliation deterministic.