# K3s Monitoring Stack Status

## Overview
The monitoring stack for the K3s cluster is now operational with the following components:

### Components Running
1. **Prometheus** - http://localhost:9090
   - Collects metrics from various sources
   - Configuration: `monitoring/prometheus.yml` (simplified)
   - Scrape interval: 15s

2. **Grafana** - http://localhost:3000
   - Username: admin
   - Password: admin
   - Dashboard: K3s Cluster Overview
   - URL: http://localhost:3000/d/bc7d997f-de62-4029-800b-588d05f52981/k3s-cluster-overview

3. **Node Exporter** - http://localhost:9100
   - Exposes host metrics
   - Collected by Prometheus as job "node-exporter-host"

4. **kube-state-metrics** - http://localhost:8081
   - Exposes Kubernetes object state
   - Runs outside cluster, collects via kubectl
   - Collected by Prometheus as job "kube-state-metrics"

## Available Metrics

### Node Metrics
- **Node Info**: `kube_node_info` - Shows all nodes (master + workers)
  - Currently showing 3 nodes: k3s-master, k3s-worker-1, k3s-worker-2

- **Node Status**: `kube_node_status_condition`
  - Conditions: Ready, DiskPressure, MemoryPressure, PIDPressure
  - Status: true/false

- **CPU Usage**: `node_cpu_seconds_total` (from node-exporter)
  - Can calculate CPU usage by subtracting idle time
  - Query: `100 - (irate(node_cpu_seconds_total{mode="idle"}[5m]) * 100)`

- **Memory Usage**: `node_memory_MemAvailable_bytes` (from node-exporter)
  - Calculate usage: `(1 - (MemAvailable / MemTotal)) * 100`

### Pod Metrics
- **Pod Status**: `kube_pod_status_phase`
  - Phases: Pending, Running, Succeeded, Failed, Unknown

- **Pod Info**: `kube_pod_info`
  - Total pod count

### Container Metrics
- **Container Status**: `kube_pod_container_status_*`
  - Ready, Running, Terminated, Waiting
- **Container Restarts**: `kube_pod_container_status_restarts_total`

## Grafana Dashboard

The K3s Cluster Overview dashboard provides:

1. **Top Row Stats**:
   - Running Nodes (worker nodes only)
   - Ready Nodes
   - Average CPU Usage (%)
   - Average Memory Usage (%)

2. **Middle Section**:
   - Pod Count by Phase (pie chart)
   - Pending Pods count

3. **Bottom Section**:
   - Node CPU Usage Over Time
   - Node Memory Usage Over Time

## Known Issues

1. **kube-state-metrics runs externally**: Since node-exporter is not deployed within the K3s cluster, kube-state-metrics runs as an external container using kubectl to access the cluster.

2. **Individual node metrics limitation**: Current node-exporter setup only provides host-level metrics, not individual node metrics from within the cluster.

3. **Network configuration**: All monitoring components run on the Docker network `prototype_k3s-network`.

## Configuration Files

- `monitoring/prometheus.yml` - Prometheus configuration
- `monitoring/grafana/provisioning/` - Grafana datasources
- `docker-compose-with-db.yml` - Service definitions
- `import-dashboard.sh` - Script to import the dashboard

## Next Steps

1. Deploy node-exporter as a DaemonSet inside the K3s cluster to get per-node metrics
2. Add more comprehensive alerting rules
3. Create additional dashboards for detailed monitoring
4. Set up persistent storage for metrics retention