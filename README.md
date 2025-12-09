# K3s Docker Autoscaler

A custom autoscaler that dynamically adds/removes Docker containers running k3s worker nodes based on Prometheus metrics.

## Architecture

- **k3s Master**: Runs the Kubernetes control plane
- **k3s Workers**: Docker containers that join the cluster as worker nodes
- **Prometheus**: Collects metrics from the cluster
- **Autoscaler**: Python service that makes scaling decisions
- **Grafana**: Visualizes metrics and scaling events

## Quick Start

1. **Start the cluster**:
   ```bash
   docker-compose up -d
   ```

2. **Check cluster status**:
   ```bash
   # Check k3s nodes
   docker exec k3s-master kubectl get nodes

   # Check all services
   docker-compose ps
   ```

3. **Deploy test workloads** (to trigger autoscaling):
   ```bash
   cd workloads
   ./deploy-workloads.sh
   ```

4. **Monitor autoscaling**:
   - **Autoscaler logs**: `docker logs -f autoscaler`
   - **Grafana dashboard**: http://localhost:3000 (admin/admin)
   - **Prometheus**: http://localhost:9090
   - **Autoscaler API**: http://localhost:8080/health

5. **Check cluster nodes**:
   ```bash
   # Watch nodes join/leave
   watch -n 5 'docker exec k3s-master kubectl get nodes'
   ```

## Configuration

Edit `autoscaler/config/config.yaml` to adjust:

- **Scaling thresholds**: CPU/Memory percentages that trigger scaling
- **Pending pod threshold**: Number of pending pods before scaling up
- **Node limits**: Minimum/maximum number of worker nodes
- **Cooldown periods**: Time between scaling operations
- **Dry run mode**: Set to `true` to simulate scaling without actual changes

## How It Works

1. **Metrics Collection**: The autoscaler queries Prometheus for:
   - Pending pods count
   - Node count
   - Average CPU utilization
   - Average memory utilization

2. **Scaling Decision**:
   - **Scale Up**: When pending pods ≥ threshold OR CPU/Memory ≥ thresholds
   - **Scale Down**: When no pending pods AND CPU/Memory < scale-down thresholds

3. **Node Management**:
   - **Scale Up**: Creates new Docker container running k3s agent
   - **Scale Down**: Drains node and removes container

## Test Workloads

The `workloads/test-workload.yaml` contains:

- **cpu-stress**: Pods that consume CPU to test resource-based scaling
- **pending-pod-test**: Pods with high resource requests that will stay pending

## Ports

| Service    | Port  | Description                    |
|------------|-------|--------------------------------|
| k3s API    | 6443  | Kubernetes API server          |
| Prometheus | 9090  | Metrics collection             |
| Grafana    | 3000  | Visualization dashboard        |
| Autoscaler | 8080  | Health check API               |
| Autoscaler | 9091  | Prometheus metrics endpoint    |

## Troubleshooting

1. **Autoscaler not scaling**:
   - Check logs: `docker logs autoscaler`
   - Verify Prometheus is accessible from autoscaler container
   - Check if metrics are being collected

2. **Workers not joining cluster**:
   - Check k3s-master logs: `docker logs k3s-master`
   - Verify network connectivity between containers
   - Check K3S_TOKEN matches between master and workers

3. **High resource usage**:
   - Scale down workloads: `kubectl delete -f workloads/test-workload.yaml`
   - Check Grafana for resource utilization trends

## Clean Up

```bash
# Stop and remove all containers
docker-compose down -v

# Remove all images (optional)
docker rmi $(docker images "k3s*" -q)
```