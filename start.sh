#!/bin/bash

# Script to start the k3s autoscaler system with proper environment variable substitution

# Set default K3S_SERVER_HOST if not provided
K3S_SERVER_HOST=${K3S_SERVER_HOST:-k3s-master}

# Process kubeconfig with environment variable substitution
echo "Processing kubeconfig with K3S_SERVER_HOST=$K3S_SERVER_HOST"
cp kubeconfig/kubeconfig.template kubeconfig/kubeconfig.temp
sed -i "s/__K3S_SERVER_HOST__/$K3S_SERVER_HOST/g" kubeconfig/kubeconfig.temp
mv kubeconfig/kubeconfig.temp kubeconfig/kubeconfig

# Start docker-compose
export K3S_SERVER_HOST
docker-compose up -d

echo "Waiting for services to start..."
sleep 30

# Check status
echo "Checking node status..."
docker exec k3s-master kubectl get nodes

echo "System started successfully!"