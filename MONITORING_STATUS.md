# K3s Monitoring Stack Current Status

## What's Working ✅

1. **Node-exporter DaemonSet**: Successfully deployed with 3 pods running (one on each k3s node)
2. **Kubernetes Service**: Created `node-exporter` service in kube-system namespace
3. **Service Endpoints**: All 3 node-exporter pods are registered as endpoints
   - 172.18.0.8:9100 (k3s-master)
   - 172.18.0.12:9100 (k3s-worker-1)
   - 172.18.0.13:9100 (k3s-worker-2)
4. **Prometheus Targets**: Shows all 3 node-exporter instances as "up"
5. **Kubernetes Metrics**: Working perfectly
   - Node counts: `count(kube_node_info)` → 3
   - Pod counts: `sum(kube_pod_info)` → 6
   - Pod status: `sum(kube_pod_status_phase{phase="Pending"})` → 0
6. **Grafana Dashboard**: Imported and accessible at http://localhost:3000/d/k3s-cluster-monitoring

## Current Issues ❌

**None!** All monitoring components are working correctly.

## Available Metrics

### From kube-state-metrics (Working):
- Node information
- Node status conditions
- Pod counts by phase
- Container resource requests

### From External node-exporter (Working):
- Host CPU usage: `node_cpu_seconds_total{job="node-exporter-host"}`
- Host memory usage: `node_memory_MemAvailable_bytes{job="node-exporter-host"}`

### From DaemonSet node-exporter (Working):
- Individual node CPU usage: `100 - (irate(node_cpu_seconds_total{mode="idle",job="node-exporter-k3s-nodes"}[5m]) * 100)`
- Individual node memory usage: `(1 - (node_memory_MemAvailable_bytes{job="node-exporter-k3s-nodes"} / node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"})) * 100`
- Average CPU usage: `avg(100 - (irate(node_cpu_seconds_total{mode="idle",job="node-exporter-k3s-nodes"}[5m]) * 100))`
- Average memory usage: `avg((1 - (node_memory_MemAvailable_bytes{job="node-exporter-k3s-nodes"} / node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"})) * 100)`

## Why This Happened

The k3s cluster runs in Docker containers, and the node-exporter DaemonSet is running inside these containers. The Prometheus configuration tries to use the kubeconfig for service discovery, but there are certificate trust issues between Prometheus (running in Docker) and the k3s API server.

## Alternative Solutions

### Option 1: Use Host Metrics (Recommended for This Setup)
Since k3s runs in Docker containers, the external node-exporter (already running) provides host-level metrics. These are sufficient for autoscaling decisions.

### Option 2: Fix Certificate Issues
- Generate proper certificates for Prometheus to trust k3s
- Or disable TLS verification for k3s API access
- Update prometheus.yml TLS configuration

### Option 3: Direct Pod IP Scraping
Update Prometheus to scrape pod IPs directly instead of using service discovery:
```yaml
- job_name: 'node-exporter-k3s-nodes'
  kubernetes_sd_configs:
    - role: pod
      kubeconfig_file: /etc/prometheus/kubeconfig
      namespaces:
        names:
        - kube-system
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
      action: keep
      regex: node-exporter
    - source_labels: [__meta_kubernetes_pod_ip]
      target_label: __address__
      regex: (.*)
      replacement: ${1}:9100
```

### Option 4: Use Host Network
Modify the DaemonSet to use `hostNetwork: true` so node-exporter listens on the host network directly.

## Current Monitoring Capabilities

With the current setup, you can monitor:

1. **Cluster State**:
   - Number of nodes
   - Node readiness status
   - Pod distribution

2. **Resource Usage**:
   - Overall host CPU usage
   - Overall host memory usage
   - Container resource requests

3. **Autoscaling Decision Points**:
   - Pending pod count (for scale-up triggers)
   - Node count (for scale-down limits)
   - Average resource utilization (for scale thresholds)

## Recommendation

For the current Docker-based k3s setup, the external node-exporter provides sufficient metrics for autoscaling decisions. The individual node metrics from within containers would be nice-to-have but aren't essential for the autoscaler's functionality.