#!/bin/bash
# Script to add a new k3s worker with node-exporter
# Usage: ./add_worker.sh <worker-number>

WORKER_NUM=$1
if [ -z "$WORKER_NUM" ]; then
    echo "Usage: $0 <worker-number>"
    exit 1
fi

WORKER_NAME="k3s-worker-$WORKER_NUM"
NODE_EXPORTER_NAME="node-exporter-$WORKER_NUM"

# Get the next available port for node-exporter
NODE_EXPORTER_PORT=$((9100 + WORKER_NUM))

# Create the worker container
docker run -d \
    --name $WORKER_NAME \
    --hostname $WORKER_NAME \
    --privileged \
    --network k3s-network \
    -e K3S_URL=https://k3s-master:6443 \
    -e K3S_TOKEN=mysupersecrettoken12345 \
    -e K3S_NODE_NAME=$WORKER_NAME \
    -e K3S_WITH_NODE_ID=true \
    -v k3s-worker-${WORKER_NUM}-data:/var/lib/rancher/k3s \
    -v /mnt/e/custom_autoscaler/prototype/kubeconfig:/output \
    rancher/k3s:v1.29.1-k3s1 agent

# Create the node-exporter container
docker run -d \
    --name $NODE_EXPORTER_NAME \
    --network k3s-network \
    -p ${NODE_EXPORTER_PORT}:9100 \
    -v /proc:/host/proc:ro \
    -v /sys:/host/sys:ro \
    -v /:/rootfs:ro \
    --pid=container:$WORKER_NAME \
    prom/node-exporter:v1.6.1 \
    --path.procfs=/host/proc \
    --path.rootfs=/rootfs \
    --path.sysfs=/host/sys \
    --collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($$|/)

# Update the k3s-nodes.yml file for Prometheus
NEW_TARGET="'$WORKER_NAME:9100'"
sed -i "/targets:/a\  - $NEW_TARGET" /mnt/e/custom_autoscaler/prototype/monitoring/k3s-nodes.yml

# Reload Prometheus configuration
curl -X POST http://localhost:9090/-/reload

echo "Added worker: $WORKER_NAME with node-exporter on port $NODE_EXPORTER_PORT"