# In-Memory Data Storage Analysis Report

## Summary
This report identifies all instances where crucial data is stored in memory instead of Redis, which can lead to data loss during restarts and inconsistencies across multiple autoscaler instances.

## Critical Findings

### 1. K3sAutoscaler Class (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/core/autoscaler.py`)

#### 1.1 Cooldown Timers (Lines 61-62)
```python
self.last_scale_up = None
self.last_scale_down = None
```
**Issue**: Cooldown timestamps stored in memory only
**Impact**: Cooldown periods reset on restart, potentially causing rapid scaling
**Fix**: Already tracked in Redis via `database.set_cooldown()`, but duplicate in-memory tracking should be removed

#### 1.2 Optimal State Emission Cooldown (Line 69)
```python
self._last_optimal_state_emission = None
```
**Issue**: Prevents duplicate optimal state events only in memory
**Impact**: Lost on restart, may cause duplicate optimal state events
**Fix**: Store in Redis with key `autoscaler:last_optimal_state_emission`

#### 1.3 Worker Prefix (Line 60)
```python
self.worker_prefix = settings.autoscaler.worker_prefix
```
**Issue**: Config stored as instance variable (acceptable as it's configuration)

### 2. Event Handlers (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/events/handlers.py`)

#### 2.1 OptimalStateHandler Class (Lines 33-34)
```python
self.optimal_state_periods = []
self.last_optimal_time: Optional[datetime] = None
```
**Issue**: Historical optimal state periods tracked only in memory
**Impact**: Analytics and trend data lost on restart
**Fix**: Store in Redis list `handler:optimal_state:periods` and hash `handler:optimal_state:last_time`

#### 2.2 ScalingCompletedHandler Class (Lines 91-94)
```python
self.scaling_history = []
self.last_scale_time: Optional[datetime] = None
self.scale_up_count = 0
self.scale_down_count = 0
```
**Issue**: Scaling history and counters stored only in memory
**Impact**: Historical scaling data lost on restart
**Fix**:
- Store in Redis list `handler:scaling:history`
- Store counters in Redis `handler:scaling:count:up` and `handler:scaling:count:down`

#### 2.3 ClusterStateSyncedHandler Class (Lines 168-169)
```python
self.last_sync_time: Optional[datetime] = None
self.sync_history = []
```
**Issue**: Sync tracking only in memory
**Impact**: Sync history lost on restart
**Fix**: Store in Redis `handler:sync:last_time` and list `handler:sync:history`

### 3. AsyncScalingManager (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/core/async_scaling.py`)

#### 3.1 Operation Tracking (Lines 43-44)
```python
self.active_operations = {}
self.operation_history = []
```
**Issue**: Active operations and history tracked only in memory
**Impact**: Cannot track operations across restarts
**Fix**: Store in Redis hash `async_scaling:active_operations` and list `async_scaling:operation_history`

### 4. MinimumNodeEnforcer Service (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/services/minimum_node_enforcer.py`)

#### 4.1 State Tracking (Lines 41-43)
```python
self.last_check: Optional[datetime] = None
self.violations_count = 0
self.enforcements_count = 0
```
**Issue**: Monitoring statistics stored only in memory
**Impact**: Historical enforcement data lost on restart
**Fix**: Store in Redis:
- `min_enforcer:last_check`
- `min_enforcer:violations_count`
- `min_enforcer:enforcements_count`

#### 4.2 Worker Count Reference (Line 110)
```python
current_workers = len(self.autoscaler.worker_nodes)
```
**Issue**: Accessing private worker_nodes list directly
**Fix**: Use `self.autoscaler.database.get_worker_count()` instead

### 5. Database Manager (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/database/manager.py`)

#### 5.1 Worker Node Tracking (Lines 53-56)
```python
self.workers = None
self.events = None
self.state = None
self.rules = None
```
**Issue**: Repository instances (acceptable - these are database connections, not data storage)

### 6. EventBus (`/mnt/e/custom_autoscaler/prototype/autoscaler/src/events/event_bus.py`)

#### 6.1 Subscriber Registry (Line 29)
```python
self.subscribers: Dict[EventType, List[EventHandler]] = defaultdict(list)
```
**Issue**: Event subscriptions stored only in memory
**Impact**: Event handlers lost on restart
**Fix**: This is acceptable as subscriptions are re-established on startup

## Recommended Redis Key Schema

### Cooldown Management
- `cooldown:scale_up` - Timestamp until cooldown expires
- `cooldown:scale_down` - Timestamp until cooldown expires

### Event Handler State
- `handler:optimal_state:last_time` - Last optimal state timestamp
- `handler:optimal_state:periods` - Redis list of optimal state periods
- `handler:scaling:last_time` - Last scaling action timestamp
- `handler:scaling:count:up` - Total scale-up operations
- `handler:scaling:count:down` - Total scale-down operations
- `handler:scaling:history` - Redis list of scaling events
- `handler:sync:last_time` - Last sync timestamp
- `handler:sync:history` - Redis list of sync events

### Operation Tracking
- `async_scaling:active_operations` - Hash of active operations
- `async_scaling:operation_history` - Redis list of operation history

### Enforcement Statistics
- `min_enforcer:last_check` - Last check timestamp
- `min_enforcer:violations_count` - Total violations
- `min_enforcer:enforcements_count` - Total enforcements

### Autoscaler State
- `autoscaler:last_optimal_state_emission` - Last optimal state emission
- `autoscaler:permanent_workers` - JSON list of permanent worker names
- `workers:counter` - Next worker number ID
- `workers:{worker_name}` - Hash with worker metadata (permanent, type, created_at)

## Implementation Priority

### High Priority (Critical)
1. Remove duplicate cooldown tracking in K3sAutoscaler (already in Redis)
2. Store optimal state emission timestamp in Redis
3. Store scaling history and counters in Redis
4. Store active operations in Redis

### Medium Priority (Important)
1. Store optimal state periods for analytics
2. Store sync history and statistics
3. Store minimum node enforcer statistics
4. Store operation history

### Low Priority (Optional)
1. Store subscriber registry (not critical as it's re-initialized)
2. Store performance metrics

## Code Changes Required

1. **Remove or migrate duplicate in-memory cooldown tracking**:
   - Remove `self.last_scale_up` and `self.last_scale_down` from K3sAutoscaler
   - Use Redis methods consistently

2. **Add Redis storage for critical event handler data**:
   - Implement Redis storage in event handlers
   - Add methods to persist and restore state

3. **Implement Redis operations for AsyncScalingManager**:
   - Store active operations in Redis hash
   - Store operation history in Redis list

4. **Update MinimumNodeEnforcer**:
   - Store statistics in Redis
   - Use database methods for worker counts

5. **Add initialization methods**:
   - Add `restore_from_redis()` methods to restore state on startup
   - Call these methods during service initialization

## Benefits of Implementation

1. **Persistence**: All critical data survives restarts
2. **Consistency**: Multiple autoscaler instances share state
3. **Analytics**: Historical data preserved for monitoring
4. **Reliability**: No loss of important state information
5. **Scalability**: Can run multiple autoscaler replicas safely