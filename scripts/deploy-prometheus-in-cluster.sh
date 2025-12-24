#!/bin/bash
# Deploy Prometheus inside the Kubernetes cluster

set -e

# Change to script directory first
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SCRIPT_DIR="$(pwd)"
MANIFESTS_DIR="$SCRIPT_DIR/monitoring/prometheus-in-cluster"
CONTAINER_MANIFESTS_DIR="/tmp/prometheus-in-cluster"

echo "============================================"
echo "Deploying Prometheus in-cluster"
echo "============================================"
echo "Working directory: $SCRIPT_DIR"
echo "Manifests directory: $MANIFESTS_DIR"

# Check if k3s-master is running
if ! docker ps | grep -q k3s-master; then
    echo "Error: k3s-master is not running"
    echo "Please start the cluster first: docker-compose -f docker-compose-with-db.yml up -d k3s-master"
    exit 1
fi

# Copy manifests into the container
echo ""
echo "Copying manifests into k3s-master container..."
docker exec k3s-master mkdir -p "$CONTAINER_MANIFESTS_DIR"
docker cp "$MANIFESTS_DIR/00-namespace.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/01-serviceaccount.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/02-configmap-prometheus.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/02-configmap-rules.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/03-pvc.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/04-deployment.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"
docker cp "$MANIFESTS_DIR/05-service.yaml" k3s-master:"$CONTAINER_MANIFESTS_DIR/"

# Create namespace and all resources
echo ""
echo "Applying Prometheus manifests..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/00-namespace.yaml"

echo ""
echo "Applying ServiceAccount and RBAC..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/01-serviceaccount.yaml"

echo ""
echo "Applying ConfigMaps..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/02-configmap-prometheus.yaml"
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/02-configmap-rules.yaml"

echo ""
echo "Applying PVC..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/03-pvc.yaml"

echo ""
echo "Applying Deployment..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/04-deployment.yaml"

echo ""
echo "Applying Services..."
docker exec k3s-master kubectl apply -f "$CONTAINER_MANIFESTS_DIR/05-service.yaml"

echo ""
echo "============================================"
echo "Waiting for Prometheus pod to be ready..."
echo "============================================"

# Wait for Prometheus pod to be ready
echo "Waiting for Prometheus deployment to roll out..."
docker exec k3s-master kubectl rollout status deployment/prometheus -n monitoring --timeout=120s

echo ""
echo "============================================"
echo "Prometheus deployed successfully!"
echo "============================================"
echo ""
echo "Access Prometheus:"
echo "  - Internal (from cluster): http://prometheus.monitoring.svc.cluster.local:9090"
echo "  - External (via NodePort): http://localhost:30900"
echo ""
echo "Check pod status:"
echo "  docker exec k3s-master kubectl get pods -n monitoring"
echo ""
echo "View logs:"
echo "  docker exec k3s-master kubectl logs -f deployment/prometheus -n monitoring"
echo ""
