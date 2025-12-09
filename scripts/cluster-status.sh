#!/bin/bash

# Helper script to check k3s cluster status without local kubectl

echo "=== K3s Cluster Status ==="
echo

# Check if containers are running
echo "Container Status:"
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "(k3s|autoscaler|prometheus|grafana)"

echo
echo "=== Kubernetes Nodes ==="
docker exec k3s-master kubectl get nodes -o wide

echo
echo "=== System Pods ==="
docker exec k3s-master kubectl get pods -n kube-system

echo
echo "=== Autoscaler Logs (last 10 lines) ==="
docker logs --tail 10 autoscaler 2>/dev/null || echo "Autosaler container not found"

echo
echo "=== Useful Commands ==="
echo "View all logs:     docker-compose logs -f"
echo "Check pods:        docker exec k3s-master kubectl get pods --all-namespaces"
echo "Scale up workloads: cd workloads && ./deploy-workloads.sh"
echo "Stop cluster:      docker-compose down"