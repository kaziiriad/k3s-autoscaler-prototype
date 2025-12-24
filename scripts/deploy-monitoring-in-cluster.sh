#!/bin/bash
# Deploy all monitoring components inside the Kubernetes cluster

set -e

# Change to script directory first
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SCRIPT_DIR="$(pwd)"
CONTAINER_MANIFESTS_DIR="/tmp/monitoring-in-cluster"

echo "============================================"
echo "Deploying Monitoring In-Cluster"
echo "============================================"
echo "Working directory: $SCRIPT_DIR"

# Check if k3s-master is running
if ! docker ps | grep -q k3s-master; then
    echo "Error: k3s-master is not running"
    echo "Please start the cluster first: docker-compose -f docker-compose-with-db.yml up -d k3s-master"
    exit 1
fi

# Create directory in container
echo ""
echo "Copying manifests into k3s-master container..."
docker exec k3s-master mkdir -p "$CONTAINER_MANIFESTS_DIR"

# Copy monitoring manifests
docker cp "$SCRIPT_DIR/monitoring/node-exporter-daemonset.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/node-exporter-service.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/kube-state-metrics.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"

# Copy prometheus manifests
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/00-namespace.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/01-serviceaccount.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/02-configmap-prometheus.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/02-configmap-rules.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/03-pvc.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/04-deployment.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$SCRIPT_DIR/monitoring/prometheus-in-cluster/05-service.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"

# Create namespace
echo ""
echo "Creating monitoring namespace..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/00-namespace.yaml"

# Deploy node-exporter DaemonSet
echo ""
echo "Deploying node-exporter DaemonSet..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/node-exporter-daemonset.yaml"

# Deploy node-exporter Service
echo ""
echo "Deploying node-exporter Service..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/node-exporter-service.yaml"

# Deploy kube-state-metrics
echo ""
echo "Deploying kube-state-metrics..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/kube-state-metrics.yaml"

# Deploy Prometheus
echo ""
echo "Deploying Prometheus ServiceAccount and RBAC..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/01-serviceaccount.yaml"

echo ""
echo "Deploying Prometheus ConfigMaps..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/02-configmap-prometheus.yaml"
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/02-configmap-rules.yaml"

echo ""
echo "Deploying Prometheus PVC..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/03-pvc.yaml"

echo ""
echo "Deploying Prometheus Deployment..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/04-deployment.yaml"

echo ""
echo "Deploying Prometheus Services..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/05-service.yaml"

echo ""
echo "Waiting for Prometheus pod to be ready..."
docker exec k3s-master kubectl rollout status deployment/prometheus -n monitoring --timeout=120s

echo ""
echo "============================================"
echo "Monitoring Deployment Complete!"
echo "============================================"
echo ""
echo "Components deployed:"
echo "  - node-exporter (DaemonSet on all nodes)"
echo "  - kube-state-metrics"
echo "  - Prometheus (on k3s-master)"
echo ""
echo "Access Prometheus:"
echo "  - Internal: http://prometheus.monitoring.svc.cluster.local:9090"
echo "  - External: http://localhost:30900"
echo ""
echo "Check status:"
echo "  docker exec k3s-master kubectl get pods -n monitoring"
echo "  docker exec k3s-master kubectl get svc -n monitoring"
echo ""
