# Implementation Guide - K3s Autoscaler Prototype

## Step 1: Project Setup

First, create the basic structure and files:

```bash
# Create directories
mkdir -p prototype/{autoscaler,config,docker,k8s,scripts,tests}

# Create essential files
touch prototype/autoscaler/{__init__.py,main.py,metrics.py,decision.py,aws_mock.py}
touch prototype/requirements.txt
touch prototype/config/{config.yaml,prometheus.yml}
touch prototype/docker/{Dockerfile,docker-compose.yml}
touch prototype/k8s/{namespace.yaml,deployment.yaml}
touch prototype/scripts/{deploy.sh,test.sh}
```

## Step 2: Core Components to Implement

### 2.1 Configuration (`config/config.yaml`)

Create a simple YAML config file. This defines:
- Check intervals
- Scaling thresholds
- Node limits
- Mock AWS settings

**Your tasks:**
- Define the structure
- Include autoscaler settings (check_interval, dry_run)
- Add thresholds (pending_pods, cpu_threshold)
- Set limits (min_nodes, max_nodes)

### 2.2 Metrics Collector (`autoscaler/metrics.py`)

This component simulates Prometheus queries.

**Key functions to implement:**
- `get_current_metrics()` - Returns dict with:
  - `pending_pods`: Number of pending pods
  - `nodes`: Current node count
  - `avg_cpu`: Average CPU utilization
  - `avg_memory`: Average memory usage

**Tips:**
- For testing, return mock data or use Kubernetes Python client
- Simulate changing metrics by incrementing values
- Add random variations to make it realistic

### 2.3 Decision Engine (`autoscaler/decision.py`)

This implements the scaling logic.

**Key function:**
- `evaluate(metrics, current_nodes)` -> Returns: "scale_up", "scale_down", or "no_action"

**Logic to implement:**
```python
if pending_pods > threshold AND nodes < max_nodes:
    return "scale_up"
elif avg_cpu < 20 AND avg_memory < 20 AND nodes > min_nodes:
    return "scale_down"
else:
    return "no_action"
```

### 2.4 Mock AWS Service (`autoscaler/aws_mock.py`)

Simulates AWS EC2 operations.

**Key functions:**
- `launch_instance()` -> Returns instance ID
- `terminate_instance()` -> Returns instance ID
- Add delays to simulate real AWS latency

### 2.5 Main Autoscaler (`autoscaler/main.py`)

The orchestrator that ties everything together.

**Flow:**
1. Load config
2. Initialize components
3. Start main loop:
   - Collect metrics
   - Make decision
   - Execute action
   - Sleep for interval

## Step 3: Dependencies (`requirements.txt`)

You'll need:
- `pyyaml` - Config parsing
- `prometheus-client` - Export metrics
- `kubernetes` - Optional: Real k8s client
- `requests` - HTTP client
- `python-dateutil` - Date/time utilities

## Step 4: Testing Your Implementation

### 4.1 Unit Tests (`tests/`)

Test each component:
- Test metrics collector with mock data
- Test decision engine with various scenarios
- Test AWS mock service

### 4.2 Integration Testing

Create test scenarios:
1. **Normal load**: No scaling needed
2. **High load**: Should trigger scale up
3. **Low load**: Should trigger scale down
4. **Boundary conditions**: At min/max limits

### 4.3 Manual Testing

1. Run locally: `python autoscaler/main.py`
2. Watch logs for scaling decisions
3. Adjust config to test different scenarios

## Step 5: Docker Setup

### 5.1 Dockerfile

Multi-stage build:
```dockerfile
FROM python:3.9-slim as builder
# Install dependencies

FROM python:3.9-slim as runtime
# Copy code and run
```

### 5.2 Docker Compose

Services to include:
- autoscaler (your Python app)
- prometheus (metrics storage)
- grafana (visualization)
- Optional: mock k3s or minikube

## Step 6: Kubernetes Deployment

### 6.1 Kubernetes Manifests

Create:
- `namespace.yaml` - Isolate the autoscaler
- `deployment.yaml` - Deploy the autoscaler pod
- `configmap.yaml` - Provide configuration
- `service.yaml` - Expose metrics endpoint

### 6.2 RBAC

Create ServiceAccount and permissions for:
- Reading nodes and pods
- Creating events

## Step 7: Implementation Tips

### 7.1 Start Simple

```python
# First version - just print metrics
def get_current_metrics():
    return {
        'pending_pods': random.randint(0, 10),
        'nodes': 1,
        'avg_cpu': random.uniform(0, 100)
    }
```

### 7.2 Add Logging

Use Python's logging module:
```python
import logging
logger = logging.getLogger(__name__)
logger.info(f"Scaling decision: {decision}")
```

### 7.3 Error Handling

Wrap external calls in try/catch:
```python
try:
    metrics = self.metrics_collector.get_current_metrics()
except Exception as e:
    logger.error(f"Failed to get metrics: {e}")
    return
```

### 7.4 State Management

Track:
- last_scale_time (for cooldowns)
- current_node_count
- scaling_history (list of events)

## Step 8: Debugging Tips

1. **Use print statements** liberally at first
2. **Check config loading** - ensure YAML is parsed correctly
3. **Verify metrics** - make sure they change as expected
4. **Test decisions** - try different metric values
5. **Mock AWS** - ensure it returns instance IDs

## Step 9: Enhancements (After Basic Version)

1. **Real Prometheus Integration**
   - Use Prometheus Python client
   - Query real metrics from your cluster

2. **Real k3s Integration**
   - Use Kubernetes Python client
   - Actually count pods and nodes

3. **Cooldown Logic**
   - Prevent rapid scaling
   - Track last scale time

4. **Web UI**
   - Flask or FastAPI endpoint
   - Show current status and history

5. **Better Mocks**
   - Simulate instance boot time
   - Add failure scenarios

## Testing Scenarios

Create these test functions in `tests/test_decision.py`:

```python
def test_scale_up_on_high_pending_pods():
    metrics = {'pending_pods': 10, 'nodes': 2}
    decision = decision_engine.evaluate(metrics, 2)
    assert decision == "scale_up"

def test_no_scale_at_max_nodes():
    metrics = {'pending_pods': 10, 'nodes': 3}
    decision = decision_engine.evaluate(metrics, 3)
    assert decision == "no_action"

def test_scale_down_on_low_utilization():
    metrics = {'pending_pods': 0, 'avg_cpu': 10}
    decision = decision_engine.evaluate(metrics, 3)
    assert decision == "scale_down"
```

## Common Pitfalls

1. **Infinite loops** - Ensure sleep in main loop
2. **Config errors** - Validate YAML syntax
3. **Type errors** - Ensure metrics are numbers
4. **State bugs** - Reset state between tests
5. **Import errors** - Check file structure

## Next Steps After Prototype

1. Validate concepts work
2. Test edge cases
3. Refine algorithms
4. Convert to Go for production
5. Add real AWS integration
6. Implement full security model

Remember: This is a learning prototype. Keep it simple and focus on understanding the concepts!