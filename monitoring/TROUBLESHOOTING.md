# K3s Monitoring Stack Troubleshooting

## Current Status
- ✅ Node-exporter DaemonSet deployed successfully (3 pods running)
- ✅ Prometheus is scraping the node-exporter endpoints
- ✅ Kubernetes metrics (pod counts, node info) are working
- ❌ Individual node CPU and memory metrics not accessible

## Issues Identified

### 1. Node Exporter Metrics Not Available
The verification script shows that node-exporter pods are running and Prometheus reports them as "up", but CPU and memory metrics queries return no results.

**Possible Causes:**
- Node-exporter is listening on IPv6 (`[::]:9100`) instead of IPv4
- Prometheus is configured to scrape Kubernetes service endpoints, but the DaemonSet creates pods directly
- Metrics might be collected under a different job name

### 2. Prometheus Configuration
The current prometheus.yml expects a Kubernetes service for node-exporter:
```yaml
- job_name: 'node-exporter-k3s-nodes'
  kubernetes_sd_configs:
    - role: endpoints
      kubeconfig_file: /etc/prometheus/kubeconfig
      namespaces:
        names:
        - kube-system
```

But the DaemonSet doesn't create a service, so Prometheus might be scraping via host IPs instead.

## Solutions

### Option 1: Create a Node Exporter Service
Create a Kubernetes service for the node-exporter DaemonSet:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: node-exporter
  namespace: kube-system
  labels:
    app.kubernetes.io/name: node-exporter
spec:
  type: ClusterIP
  ports:
    - name: metrics
      port: 9100
      targetPort: 9100
  selector:
    app.kubernetes.io/name: node-exporter
```

### Option 2: Update Prometheus to Scrape Pod IPs Directly
Update prometheus.yml to use pod discovery instead of service discovery:

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

### Option 3: Use Host Network for Node Exporter
Update the DaemonSet to use hostNetwork: true, which will make node-exporter listen on the host's IP directly:

```yaml
spec:
  template:
    spec:
      hostNetwork: true
      hostPID: true
      hostIPC: true
```

## Current Working Metrics

The following metrics ARE working:

1. **Kubernetes Metrics from kube-state-metrics**:
   - Node count: `count(kube_node_info)`
   - Pod counts: `sum(kube_pod_info)`
   - Pod status: `sum(kube_pod_status_phase{phase="Pending"})`

2. **Basic Node Exporter from Docker Host**:
   - Host CPU: `100 - (irate(node_cpu_seconds_total{mode="idle",job="node-exporter-host"}[5m]) * 100)`
   - Host Memory: `(1 - (node_memory_MemAvailable_bytes{job="node-exporter-host"} / node_memory_MemTotal_bytes{job="node-exporter-host"})) * 100`

## Grafana Dashboard Queries

The Grafana dashboard has been updated with the following queries:

- **Total Nodes**: `count(kube_node_info)`
- **Ready Nodes**: `sum(kube_node_status_condition{condition="Ready",status="true"})`
- **Pod Status**: `sum by (phase) (kube_pod_status_phase)`

For individual node metrics, the dashboard expects:
- `job="node-exporter-k3s-nodes"`

But currently, the metrics might be under:
- `job="node-exporter-host"` (from the external node-exporter)
- Or no metrics at all from the DaemonSet

## Next Steps

1. Deploy the node-exporter service:
   ```bash
   kubectl apply -f monitoring/node-exporter-service.yaml
   ```

2. Or update the Prometheus configuration to scrape pod IPs directly

3. Restart Prometheus:
   ```bash
   docker-compose -f docker-compose-with-db.yml restart prometheus
   ```

4. Verify with:
   ```bash
   ./verify_metrics.sh
   ```

## Debug Commands

Check node-exporter pods:
```bash
docker exec k3s-master kubectl get pods -n kube-system -l app.kubernetes.io/name=node-exporter
```

Check Prometheus targets:
```bash
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.job | contains("node-exporter"))'
```

Test metrics directly from a pod:
```bash
# Get pod IP
POD_IP=$(docker exec k3s-master kubectl get pod -n kube-system -l app.kubernetes.io/name=node-exporter -o jsonpath='{.items[0].status.podIP}')
# Test metrics
curl -s http://$POD_IP:9100/metrics | head -5
```