# Node Exporter Setup Options

## Current Situation
The k3s cluster is running in Docker containers with the following IPs:
- k3s-master: 172.18.0.8
- k3s-worker-1: 172.18.0.12
- k3s-worker-2: 172.18.0.13

## Option 1: Individual Node Exporter Containers (Recommended)

Create a separate node-exporter container for each k3s node that can access the host's /proc and /sys filesystems:

```yaml
# Add to docker-compose-with-db.yml
node-exporter-k3s-master:
  image: prom/node-exporter:v1.6.1
  container_name: node-exporter-k3s-master
  pid: "host"
  network_mode: "service:k3s-master"
  command:
    - '--path.procfs=/host/proc'
    - '--path.rootfs=/host'
    - '--path.sysfs=/host/sys'
    - '--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)'
  volumes:
    - /proc:/host/proc:ro
    - /sys:/host/sys:ro
    - /:/rootfs:ro
  restart: unless-stopped

node-exporter-k3s-worker-1:
  image: prom/node-exporter:v1.6.1
  container_name: node-exporter-k3s-worker-1
  pid: "host"
  network_mode: "service:k3s-worker-1"
  command:
    - '--path.procfs=/host/proc'
    - '--path.rootfs=/host'
    - '--path.sysfs=/host/sys'
    - '--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)'
  volumes:
    - /proc:/host/proc:ro
    - /sys:/host/sys:ro
    - /:/rootfs:ro
  restart: unless-stopped

node-exporter-k3s-worker-2:
  image: prom/node-exporter:v1.6.1
  container_name: node-exporter-k3s-worker-2
  pid: "host"
  network_mode: "service:k3s-worker-2"
  command:
    - '--path.procfs=/host/proc'
    - '--path.rootfs=/host'
    - '--path.sysfs=/host/sys'
    - '--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)'
  volumes:
    - /proc:/host/proc:ro
    - /sys:/host/sys:ro
    - /:/rootfs:ro
  restart: unless-stopped
```

## Option 2: Use Docker Metrics (Simpler)

Docker already provides container metrics. We can create a metrics collector that queries Docker API for:

- Container CPU usage
- Container memory usage
- Container status
- Network stats

This can be added to the existing autoscaler's metrics collector.

## Option 3: Deploy within k3s with proper certificates

To fix the TLS issue when deploying node-exporter DaemonSet:

1. Generate proper certificates for k3s
2. Or disable TLS verification for internal services
3. Use the k3s admin config properly

```bash
# Inside k3s-master container
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl apply -f node-exporter-daemonset.yaml --validate=false
```

## Prometheus Configuration Update

Add the following to prometheus.yml after deploying node-exporter:

```yaml
- job_name: 'k3s-node-exporter'
  static_configs:
    - targets:
        - '172.18.0.8:9100'  # k3s-master
        - '172.18.0.12:9100' # k3s-worker-1
        - '172.18.0.13:9100' # k3s-worker-2
  metrics_path: /metrics
  scrape_interval: 15s
  relabel_configs:
    - source_labels: [__address__]
      regex: '172\.18\.0\.8:9100'
      target_label: node
      replacement: 'k3s-master'
    - source_labels: [__address__]
      regex: '172\.18\.0\.12:9100'
      target_label: node
      replacement: 'k3s-worker-1'
    - source_labels: [__address__]
      regex: '172\.18\.0\.13:9100'
      target_label: node
      replacement: 'k3s-worker-2'
```

## Grafana Dashboard Updates

Update queries to use node-specific metrics:

- CPU per node: `node_cpu_seconds_total{node="k3s-master"}`
- Memory per node: `node_memory_MemAvailable_bytes{node="k3s-worker-1"}`