#!/bin/bash

# Deploy test workloads to trigger autoscaling

echo "Deploying test workloads using k3s-master container..."

# Check if k3s-master is running
if ! docker ps | grep -q k3s-master; then
    echo "Error: k3s-master container is not running!"
    echo "Please start the system first with: docker-compose up -d"
    exit 1
fi

# Copy the workload file to the container
docker cp test-workload.yaml k3s-master:/tmp/test-workload.yaml

# Apply the test workloads using kubectl in the container
docker exec k3s-master kubectl apply -f /tmp/test-workload.yaml

echo "Waiting for pods to be created..."
sleep 10

# Show pod status
echo "Current pod status:"
docker exec k3s-master kubectl get pods -n workload-test

echo -e "\nTo trigger autoscaling:"
echo "1. The 'pending-pod-test' deployment creates 10 pods with high resource requests"
echo "2. These pods will likely remain in Pending state due to resource constraints"
echo "3. The autoscaler should detect pending pods and scale up the cluster"
echo -e "\nTo scale down, delete the workload:"
echo "docker exec k3s-master kubectl delete -f /tmp/test-workload.yaml"
echo -e "\nTo check cluster nodes:"
echo "docker exec k3s-master kubectl get nodes"