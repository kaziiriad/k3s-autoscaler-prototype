# Common Race Condition Scenarios & Solutions

## Scenario 1: Double Scaling (Most Critical)

### Problem
```
Time    Autoscaler A              Autoscaler B
────────────────────────────────────────────────────
T0      Check: 3 workers
T1                                Check: 3 workers
T2      Decision: Scale up!
T3                                Decision: Scale up!
T4      Create worker-4
T5                                Create worker-4  ❌ CONFLICT!
```

### Solution: Distributed Lock
```python
async def scale_up(self, count):
    # ✅ Lock prevents concurrent operations
    async with DistributedLock(redis, "scaling_operation", ttl=120):
        # Only ONE process can be here
        current = get_worker_count()
        if current < max_nodes:
            create_workers(count)
```

**Result:** Autoscaler B waits for A to finish, then re-checks conditions.

---

## Scenario 2: Counter Race (Critical)

### Problem
```
Process A                   Process B                   Counter
─────────────────────────────────────────────────────────────────
INCR counter               
→ gets 5                                                5 → 6
                           INCR counter
                           → gets 6                     6 → 7
Create k3s-worker-5
                           Create k3s-worker-6
[Worker-5 fails]
DECR counter ❌                                         7 → 6
                           [Worker-6 succeeds]
```

**Result:** Worker-6 exists but counter is 6, next worker will be worker-6 again! 💥

### Solution: Never Decrement
```python
# ✅ CORRECT: Counter only goes up
worker_num = redis.INCR("workers:next_number")  # 7
try:
    create_container(f"k3s-worker-{worker_num}")
except:
    # DON'T DECREMENT - number is retired
    logger.info(f"Number {worker_num} retired (failed creation)")
    # Next worker will be 8, not 7
```

**Result:** Counter always increases, gaps are OK (5, 6, 8, 9, ...)

---

## Scenario 3: State Inconsistency

### Problem
```
Time    Action                  Docker    K8s       Redis
────────────────────────────────────────────────────────────
T0      Reserve number          -         -         reserved
T1      Create container        running   -         creating
T2      [K8s verification fails]
T3      [Cleanup container]     removed   -         creating ❌
```

**Result:** Redis says "creating" but container doesn't exist!

### Solution: State Machine with CAS
```python
# ✅ Atomic state transitions
def create_worker():
    # State: RESERVED
    if not CAS_transition(RESERVED → CREATING):
        return False  # Another process changed state
    
    # State: CREATING
    container = create_container()
    if not CAS_transition(CREATING → VERIFYING):
        cleanup_container()
        return False
    
    # State: VERIFYING
    if verify_k8s():
        CAS_transition(VERIFYING → READY)
    else:
        CAS_transition(VERIFYING → FAILED)
        cleanup()
```

**Result:** State always matches reality, invalid transitions rejected.

---

## Scenario 4: Crash During Scale

### Problem
```
Time    Action                  State
────────────────────────────────────────
T0      INCR counter → 10       Counter: 10
T1      Create k3s-worker-10    Container: creating
T2      [CRASH] 💥              Container: orphaned
T3      [Restart]               Counter: 10
T4      Next worker = 10 ❌      Duplicate!
```

### Solution: Write-Ahead Log (WAL)
```python
# ✅ Log BEFORE action
def scale_up():
    # 1. Log intention
    op_id = wal.log("scale_up", worker_num=10)
    
    # 2. Execute
    create_worker_10()
    
    # 3. Mark complete
    wal.complete(op_id)

# On restart:
def recover():
    incomplete = wal.get_incomplete()
    for op in incomplete:
        if op.state == "creating":
            # Check if succeeded
            if docker.exists(op.worker_name):
                wal.complete(op.id)
            else:
                # Rollback
                redis.DECR(counter)  # OK here (immediate)
```

**Result:** Crash recovery ensures consistency.

---

## Scenario 5: Permanent Worker Removal

### Problem
```
Time    Event                           Permanent Workers
────────────────────────────────────────────────────────────
T0      Docker: worker-1, worker-2      [worker-1, worker-2]
T1      Stop docker containers          []
T2      Reconciliation starts           
T3      permanent = {w for w in config  
            if w in docker_containers}  permanent = {} ❌
T4      Clean Redis entries             [DELETED!]
```

### Solution: Configuration-Based Protection
```python
# ✅ Never filter by Docker state
def identify_permanent_workers(state):
    permanent = set()
    
    # From config (ALWAYS protected)
    permanent.update(config.permanent_workers)
    
    # Convention (ALWAYS protected)
    permanent.add("k3s-worker-1")
    
    # ❌ DON'T DO THIS:
    # permanent = {w for w in permanent if w in docker}
    
    return permanent  # Protected regardless of Docker state
```

**Result:** Permanent workers never removed, even if containers stopped.

---

## Quick Reference: Pattern Selection

| Scenario | Pattern | Priority | Complexity |
|----------|---------|----------|------------|
| Multiple autoscalers | Distributed Lock | 🔴 Critical | Low |
| Counter corruption | Never Decrement | 🔴 Critical | Very Low |
| State mismatch | State Machine + CAS | 🟡 High | Medium |
| Crash recovery | Write-Ahead Log | 🟡 High | Medium |
| Permanent deletion | Config-Based Protection | 🔴 Critical | Very Low |

---

## Implementation Checklist

### Must Do (Critical)
- [ ] Remove counter decrement from scale-down
- [ ] Add distributed lock around scaling operations
- [ ] Fix permanent worker identification (don't filter by Docker)
- [ ] Add state machine (at minimum: RESERVED, CREATING, READY, FAILED)

### Should Do (High Value)
- [ ] Implement CAS for state transitions
- [ ] Add Write-Ahead Log for operations
- [ ] Add crash recovery on startup
- [ ] Add counter validation in reconciliation

### Nice to Have (Medium Value)
- [ ] Lock heartbeat extension
- [ ] Operation retries with backoff
- [ ] Prometheus metrics for lock contention
- [ ] Distributed tracing

---

## Testing Your Fixes

### Test 1: No Double Scaling
```bash
# Start 2 autoscaler instances
docker run -d autoscaler:latest
docker run -d autoscaler:latest

# Trigger scale-up from both
curl http://autoscaler1:8080/scale -d '{"action":"scale_up","count":5}'
curl http://autoscaler2:8080/scale -d '{"action":"scale_up","count":5}'

# Verify: Only 5 workers created (not 10)
docker ps | grep k3s-worker | wc -l  # Should be 5
```

### Test 2: Counter Monotonicity
```bash
# Get initial counter
BEFORE=$(docker exec redis redis-cli GET autoscaler:workers:next_number)

# Scale up then down
curl -X POST http://localhost:8080/scale -d '{"action":"scale_up","count":2}'
curl -X POST http://localhost:8080/scale -d '{"action":"scale_down","count":1}'

# Check counter
AFTER=$(docker exec redis redis-cli GET autoscaler:workers:next_number)

# Counter should only increase
if [ $AFTER -lt $BEFORE ]; then
    echo "❌ FAIL: Counter decreased!"
else
    echo "✓ PASS: Counter monotonic"
fi
```

### Test 3: Crash Recovery
```bash
# Start scale-up
curl -X POST http://localhost:8080/scale -d '{"action":"scale_up","count":1}' &

# Kill autoscaler mid-operation
sleep 2
docker kill autoscaler

# Restart
docker start autoscaler

# Check logs for recovery
docker logs autoscaler | grep -i "recovering\|incomplete"
# Should see: "Found 1 incomplete operation" and recovery action
```

---

## Emergency Fixes (If Production is Broken)

### Fix 1: Stop Counter Corruption NOW
```bash
# Patch the code immediately
cd autoscaler
sed -i '/decrement_worker_counter()/d' core/async_scaling.py
docker-compose restart autoscaler
```

### Fix 2: Add Basic Lock
```python
# Minimal lock implementation
def execute_scaling(self, decision):
    lock_key = "scaling:lock"
    lock_acquired = self.redis.set(lock_key, "1", nx=True, ex=60)
    
    if not lock_acquired:
        logger.info("Another scaling in progress")
        return False
    
    try:
        # Your existing scaling code
        return self._do_scaling(decision)
    finally:
        self.redis.delete(lock_key)
```

### Fix 3: Fix Permanent Workers
```python
# Patch reconciliation
def _identify_permanent_workers(self, state):
    permanent = {"k3s-worker-1", "k3s-worker-2"}
    # DON'T filter by Docker existence
    return permanent
```

These three fixes will prevent the most critical issues immediately.
