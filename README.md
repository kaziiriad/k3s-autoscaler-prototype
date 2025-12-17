# K3s Docker Autoscaler

A production-grade autoscaler that dynamically adds/removes Docker containers running k3s worker nodes based on Prometheus metrics. Features atomic scaling operations, comprehensive logging, event-driven architecture, and state reconciliation for robust error handling.

## Architecture

### Core Components
- **k3s Master**: Runs the Kubernetes control plane
- **k3s Workers**: Docker containers that join the cluster as worker nodes
- **Prometheus**: Collects metrics from node-exporter and kube-state-metrics
- **Autoscaler**: Python service that makes scaling decisions with atomic operations
- **Grafana**: Visualizes metrics and scaling events with pre-built dashboards
- **MongoDB**: Persistent storage for scaling history and worker state
- **Redis**: Real-time caching and cooldown management

### Key Features
- ✅ **Atomic Scaling Operations**: Ensures consistency across Docker, Kubernetes, and database
- ✅ **Graceful Node Draining**: Proper pod eviction before node removal
- ✅ **Comprehensive Logging**: Visual cycle separators and section headers
- ✅ **Type Safety**: Pydantic models for metrics and configuration
- ✅ **Rollback Mechanisms**: Automatic rollback on failed operations
- ✅ **Health Monitoring**: Built-in health checks and metrics endpoints
- ✅ **Event-Driven Architecture**: Production-ready event bus with automatic recovery
- ✅ **State Reconciliation**: Continuous synchronization across all data stores
- ✅ **Redis Persistence**: All state stored in Redis for consistency across restarts
- ✅ **LIFO Scaling**: Last-In-First-Out scaling with permanent worker protection
- ✅ **Concurrent Operations**: Async scaling manager for parallel container operations
- ✅ **Namespace Support**: Workers launch in proper Docker namespace (prototype)
- ✅ **Event Bus Health Monitoring**: Automatic health checks and recovery with exponential backoff

## Quick Start

### 1. Start the complete stack
```bash
# Start all services including monitoring and database
docker-compose -f docker-compose-with-db.yml up -d

# Or use the quick start script
./start.sh
```

### 2. Verify the cluster
```bash
# Check k3s nodes (1 master + 2 workers)
docker exec k3s-master kubectl get nodes

# Check all services
docker-compose ps

# Quick status check
./scripts/cluster-status.sh
```

### 3. Access monitoring dashboards
- **Grafana Dashboard**: http://localhost:3000 (admin/admin)
  - Pre-configured k3s cluster dashboard
  - Individual node metrics visualization
- **Prometheus**: http://localhost:9090
- **Autoscaler API**: http://localhost:8080/health

### 4. Deploy test workloads (to trigger autoscaling)
```bash
cd workloads
./deploy-workloads.sh

# Or manually deploy
docker exec k3s-master kubectl apply -f cpu-stress.yaml
docker exec k3s-master kubectl scale deployment cpu-stress --replicas=10
```

### 5. Monitor autoscaling
```bash
# Watch nodes join/leave
watch -n 5 'docker exec k3s-master kubectl get nodes'

# View autoscaler logs with detailed cycle information
docker logs -f autoscaler

# Check individual node metrics
curl http://localhost:8080/nodes/metrics | python3 -m json.tool
```

## Configuration

Edit `autoscaler/config/config-with-db.yaml` to adjust:

### Core Settings
- **check_interval**: How often to check metrics (default: 5 seconds)
- **dry_run**: Set to `true` to simulate scaling without actual changes

### Scaling Thresholds
- **pending_pods**: Scale up when pods are pending (default: 1)
- **cpu_threshold**: CPU % to trigger scale-up (default: 80%)
- **memory_threshold**: Memory % to trigger scale-up (default: 80%)
- **cpu_scale_down**: CPU % to allow scale-down (default: 30%)
- **memory_scale_down**: Memory % to allow scale-down (default: 30%)

### Scaling Limits
- **min_nodes**: Minimum worker nodes (default: 2)
- **max_nodes**: Maximum worker nodes (default: 6)
- **scale_up_cooldown**: Wait time after scale-up (default: 60s)
- **scale_down_cooldown**: Wait time after scale-down (default: 120s)

## Architecture Improvements (December 2025)

### Event-Driven Architecture
The autoscaler now uses a production-ready event bus pattern for reactive operations:
- **Improved Event Bus**: Central message hub with automatic recovery and health monitoring
- **Safe Event Emission**: Thread-safe event publishing from synchronous code
- **Event Handlers**: Specialized handlers for different event types
  - `ClusterStateSynced`: Syncs cluster state with database
  - `NodeHealthDegraded`: Handles unhealthy node detection
  - `OptimalStateTrigger`: Prevents unnecessary scaling oscillations
  - `PendingPodsDetected`: Fast response to pod scheduling needs
  - `ScalingCompleted`: Handles post-scaling operations
  - `MinimumNodeEnforcement`: Ensures minimum worker count
- **Resilient Event Processing**:
  - Automatic retry with exponential backoff (max 5 retries)
  - Event queuing for sync-to-async bridge
  - Timeout protection (5s for publish, 10s for initialization)
  - Graceful degradation when event bus unavailable

### State Reconciliation System
- **Startup Reconciliation**: Automatic state sync on service start
- **Continuous Reconciliation**: Runs every 60 seconds to maintain consistency
- **Multi-Source Sync**: Synchronizes Docker, Kubernetes, Redis, and MongoDB
- **Issue Detection & Fixing**: Automatically detects and fixes:
  - Stale Redis/MongoDB entries
  - Orphaned Kubernetes nodes
  - Missing database records
  - Status mismatches
  - Worker counter inconsistencies
- **Reconciliation Monitoring**: Prometheus metrics for cycles, duration, and issues fixed

### Enhanced State Management
- **Redis-First Strategy**: All state stored in Redis for consistency
  - Worker nodes tracking
  - Cooldown periods
  - Event history
  - Minimum node enforcement statistics
- **Namespace Support**: Workers created with proper Docker labels (`com.docker.compose.project=prototype`)

### LIFO Scaling Implementation
- **Permanent Workers**: Configurable workers that are never removed (k3s-worker-1, k3s-worker-2)
- **Removable Workers**: Dynamic workers that scale based on load
- **Last-In-First-Out**: Most recently created workers are removed first
- **Atomic Operations**: Worker numbering using Redis INCR for race-free allocation

## How It Works

### 1. Metrics Collection
The autoscaler queries Prometheus for:
- **Node Status**: Current worker nodes (excluding control-plane)
- **Pending Pods**: Pods waiting to be scheduled
- **Resource Usage**: Individual node CPU and memory from node-exporter
- **Cluster Metrics**: Aggregated averages across all workers

### 2. Scaling Decision Engine
```
SCALE UP WHEN:
- Pending pods ≥ threshold (OR)
- CPU ≥ threshold AND Memory ≥ threshold

SCALE DOWN WHEN:
- No pending pods AND
- CPU < scale-down threshold AND
- Memory < scale-down threshold
- Not at minimum nodes
- Not in cooldown period
```

### 3. Atomic Scaling Process
For **Scale Up**:
1. Create Docker containers with k3s agent
2. Wait for node registration in Kubernetes
3. Verify node readiness status
4. Update database (only after successful verification)
5. Set cooldown period
6. Rollback if any step fails

For **Scale Down**:
1. Drain Kubernetes node (graceful pod eviction)
2. Remove Docker containers
3. Wait for Kubernetes node removal
4. Update database (only after successful removal)
5. Set cooldown period

## API Endpoints

### Core Endpoints
- `GET /` - Service information
- `GET /health` - Health check with database status
- `GET /metrics` - Current cluster metrics
- `GET /nodes/metrics` - Individual node metrics (like `kubectl describe nodes`)
- `GET /workers` - Worker container details
- `GET /history` - Scaling event history
- `GET /config` - Current configuration (sanitized)

### Management Endpoints
- `POST /scale` - Manual scaling
  ```bash
  curl -X POST http://localhost:8080/scale \
    -H "Content-Type: application/json" \
    -d '{"action": "scale_up", "count": 1, "reason": "Manual test"}'
  ```
- `POST /cycle` - Trigger manual scaling cycle
- `POST /stop` - Stop the autoscaler

### Reconciliation Endpoints
- `GET /reconciliation/status` - View reconciliation system status
  ```bash
  curl http://localhost:8080/reconciliation/status | python3 -m json.tool
  ```
- `POST /reconciliation/trigger` - Manually trigger a reconciliation cycle
  ```bash
  curl -X POST http://localhost:8080/reconciliation/trigger
  ```

### Prometheus Metrics
- `autoscaler_scaling_decisions_total` - Total scaling decisions
- `autoscaler_current_nodes` - Current node count
- `autoscaler_pending_pods` - Pending pod count
- `autoscaler_scale_up_events_total` - Scale-up events
- `autoscaler_scale_down_events_total` - Scale-down events
- `autoscaler_errors_total` - Error count by type

#### Reconciliation Metrics
- `autoscaler_reconciliation_cycles_total` - Total reconciliation cycles
- `autoscaler_reconciliation_duration_seconds` - Time taken for reconciliation (histogram)
- `autoscaler_reconciliation_issues_fixed_total` - Issues fixed by reconciliation (by type)

## Monitoring Stack

### Prometheus Metrics Collection
The system automatically deploys:
- **Node Exporter DaemonSet**: Collects host metrics from all nodes
- **kube-state-metrics**: Provides Kubernetes object metrics
- **Custom Autoscaler Metrics**: Exposed on port 9091

### Grafana Dashboard
Pre-configured dashboards include:
- **Cluster Dashboard**:
  - Cluster capacity and utilization
  - Individual node performance
  - Scaling events timeline
  - Resource allocation vs usage
  - Pod scheduling status
- **Reconciliation Dashboard**:
  - Reconciliation rate over time
  - Duration percentiles (50th, 95th, 99th)
  - Issues fixed by type (pie chart)
  - Issues fixed over time (stacked graph)

## Test Workloads

### CPU Stress Test
```bash
# Deploy CPU-intensive pods
docker exec k3s-master kubectl apply -f cpu-stress.yaml
docker exec k3s-master kubectl scale deployment cpu-stress --replicas=10
```

### Pending Pod Test
```bash
# Deploy pods with high resource requests
docker exec k3s-master kubectl apply -f pending-pod.yaml
docker exec k3s-master kubectl scale deployment pending-pod-test --replicas=20
```

### Clean Up Test Workloads
```bash
docker exec k3s-master kubectl delete -f workloads/
```

## Logging

### Log Format
The autoscaler provides structured logging with:
- **Cycle Separators**: Numbered separators for each autoscaling cycle
- **Section Headers**: METRICS COLLECTION, SCALING DECISION, SCALING EXECUTION
- **Colored Output**: Different colors for log levels
- **File Logging**: Optional log file persistence

### Example Log Output
```
================== AUTOSCALING CYCLE #23 ==================
--- METRICS COLLECTION [2025-12-15 09:53:05] ---
Collected metrics: 2 nodes, 0 pending, CPU: 45.2%, Memory: 67.8%

--- SCALING DECISION [2025-12-15 09:53:05] ---
Decision: No scaling (conditions not met)

--- SCALING EXECUTION [2025-12-15 09:53:05] ---
Dry-run mode: Skipping actual scaling execution
```

## Ports

| Service | Port | Description |
|---------|------|-------------|
| k3s API | 6443 | Kubernetes API server |
| Prometheus | 9090 | Metrics collection |
| Grafana | 3000 | Visualization dashboard |
| Autoscaler API | 8080 | REST API |
| Autoscaler Metrics | 9091 | Prometheus metrics |
| MongoDB | 27017 | Database |
| Redis | 6379 | Cache |

## Troubleshooting

### Common Issues

1. **Autoscaler not detecting nodes**
   ```bash
   # Check kubeconfig
   docker exec autoscaler cat /app/kubeconfig/kubeconfig

   # Test kubectl inside container
   docker exec autoscaler kubectl get nodes

   # Check logs for initialization errors
   docker logs autoscaler | grep -i error
   ```

2. **Scaling decisions not triggering**
   ```bash
   # Check current metrics
   curl -s http://localhost:8080/metrics | python3 -m json.tool

   # Check cooldown status
   curl -s http://localhost:8080/health | python3 -m json.tool

   # View scaling decisions
   docker logs autoscaler | grep -E "Scaling decision|should_scale"
   ```

3. **Workers not joining cluster**
   ```bash
   # Check k3s-master logs
   docker logs k3s-master | grep -i token

   # Check worker container logs
   docker logs k3s-worker-3

   # Verify network connectivity
   docker network inspect prototype_k3s-network
   ```

4. **High resource usage**
   ```bash
   # Scale down workloads
   docker exec k3s-master kubectl delete -f workloads/

   # Check node resource usage
   docker exec k3s-master kubectl top nodes
   ```

5. **Redis cache showing wrong worker count**
   ```bash
   # Clear Redis cache to force resync
   docker exec redis redis-cli FLUSHALL

   # Restart autoscaler to rebuild cache
   docker restart prototype-autoscaler-1

   # Verify worker detection
   docker logs prototype-autoscaler-1 | grep "Found worker node"
   ```

6. **Permanent workers not being protected**
   ```bash
   # Check permanent workers configuration
   docker exec redis redis-cli GET "autoscaler:workers:permanent"

   # Verify worker breakdown
   docker logs prototype-autoscaler-1 | grep "Worker breakdown"
   ```

7. **Reconciliation not working**
   ```bash
   # Check reconciliation status
   curl -s http://localhost:8080/reconciliation/status | python3 -m json.tool

   # Trigger manual reconciliation
   curl -X POST http://localhost:8080/reconciliation/trigger

   # Check reconciliation metrics
   curl -s http://localhost:9091/metrics | grep reconciliation
   ```

8. **Event bus issues**
   ```bash
   # Check autoscaler logs for event bus errors
   docker logs prototype-autoscaler-1 | grep -i "event bus"

   # Verify event system is initialized
   docker logs prototype-autoscaler-1 | grep "Background services started"
   ```

### Debug Mode
Enable debug logging by updating config:
```yaml
logging:
  level: "DEBUG"
  file: "/app/logs/autoscaler.log"
```

Or set environment variable:
```bash
export LOG_LEVEL=DEBUG
```

## Development

### Running in Development Mode
```bash
cd autoscaler
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run with custom config
python src/main.py --config ../config/config-with-db.yaml --dry-run
```

### Running Tests
```bash
# Unit tests
pytest tests/

# Integration tests
docker-compose -f docker-compose-test.yml up -d
```

### Database Management
```bash
# Initialize with default scaling rules
python scripts/init_database.py

# View scaling history
python scripts/view_history.py
```

## Clean Up

```bash
# Stop and remove all containers
docker-compose -f docker-compose-with-db.yml down -v

# Remove all images (optional)
docker rmi $(docker images "k3s*" -q)
docker rmi $(docker images "rancher/k3s*" -q)

# Clean up volumes
docker volume prune -f
```

## Production Deployment

When moving to production:

1. **Security**
   - Replace default credentials
   - Enable RBAC for Kubernetes API
   - Use TLS for all API communications
   - Set up network policies

2. **Reliability**
   - Deploy autoscaler in HA mode
   - Configure health checks
   - Set up proper monitoring and alerting
   - Implement backup procedures

3. **Performance**
   - Tune Prometheus scraping intervals
   - Configure retention policies
   - Optimize database queries
   - Scale monitoring components

## Architecture Diagram

See `design.excalidraw` for a visual representation of the system architecture.