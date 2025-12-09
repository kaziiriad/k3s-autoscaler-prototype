#!/bin/bash
# Script to remove a k3s worker and its node-exporter
# Usage: ./remove_worker.sh <worker-number>

WORKER_NUM=$1
if [ -z "$WORKER_NUM" ]; then
    echo "Usage: $0 <worker-number>"
    exit 1
fi

WORKER_NAME="k3s-worker-$WORKER_NUM"
NODE_EXPORTER_NAME="node-exporter-$WORKER_NUM"

# Stop and remove containers
docker stop $WORKER_NAME $NODE_EXPORTER_NAME 2>/dev/null || true
docker rm $WORKER_NAME $NODE_EXPORTER_NAME 2>/dev/null || true

# Remove the node from kubernetes
docker exec k3s-master kubectl delete node $WORKER_NAME --ignore-not-found=true

# Update the k3s-nodes.yml file for Prometheus
sed -i "/$WORKER_NAME:9100/d" /mnt/e/custom_autoscaler/prototype/monitoring/k3s-nodes.yml

# Reload Prometheus configuration
curl -X POST http://localhost:9090/-/reload

echo "Removed worker: $WORKER_NAME and its node-exporter"