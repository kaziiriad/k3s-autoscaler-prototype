#!/bin/bash

set -e

echo "========================================="
echo "K3s Monitoring Stack Setup"
echo "========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Pre-flight checks
echo -e "${YELLOW}[0/7] Running pre-flight checks...${NC}"

# Check if kubeconfig exists
if [ ! -f "./kubeconfig/kubeconfig" ]; then
    echo -e "${RED}✗ Error: kubeconfig file not found at ./kubeconfig/kubeconfig${NC}"
    echo "Waiting for K3s to generate kubeconfig..."
    sleep 10
    if [ ! -f "./kubeconfig/kubeconfig" ]; then
        echo -e "${RED}✗ Kubeconfig still not found. Make sure K3s is running.${NC}"
        exit 1
    fi
fi

# Create a temporary kubeconfig for host use (with 127.0.0.1)
echo "Creating temporary kubeconfig for host commands..."
cp ./kubeconfig/kubeconfig ./kubeconfig/kubeconfig-host.tmp
sed -i 's|server: https://k3s-master:6443|server: https://127.0.0.1:6443|g' ./kubeconfig/kubeconfig-host.tmp
sed -i 's|server: https://127.0.0.1:6443|server: https://127.0.0.1:6443|g' ./kubeconfig/kubeconfig-host.tmp

# Ensure the main kubeconfig uses k3s-master for Prometheus
if grep -q "127.0.0.1" ./kubeconfig/kubeconfig; then
    echo "Fixing kubeconfig server address for Prometheus..."
    cp ./kubeconfig/kubeconfig ./kubeconfig/kubeconfig.bak
    sed -i 's|server: https://127.0.0.1:6443|server: https://k3s-master:6443|g' ./kubeconfig/kubeconfig
fi

echo -e "${GREEN}✓ Pre-flight checks passed${NC}"
echo "  Host kubeconfig: ./kubeconfig/kubeconfig-host.tmp (server: 127.0.0.1:6443)"
echo "  Prometheus kubeconfig: ./kubeconfig/kubeconfig (server: k3s-master:6443)"
echo ""

# Step 1: Deploy node-exporter DaemonSet
echo -e "${YELLOW}[1/7] Deploying node-exporter DaemonSet to K3s...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl apply -f monitoring/node-exporter-daemonset.yaml --validate=false
echo -e "${GREEN}✓ Node-exporter DaemonSet deployed${NC}"
echo ""

# Step 2: Wait for node-exporter pods
echo -e "${YELLOW}[2/7] Waiting for node-exporter pods to be ready...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=node-exporter \
  -n kube-system \
  --timeout=120s
echo -e "${GREEN}✓ Node-exporter pods are ready${NC}"
echo ""

# Step 3: Check pod status
echo -e "${YELLOW}[3/7] Checking node-exporter pod status...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl get pods -n kube-system -l app.kubernetes.io/name=node-exporter
echo ""

# Step 4: Stop Prometheus to fix config
echo -e "${YELLOW}[4/7] Stopping Prometheus to update configuration...${NC}"
docker-compose -f docker-compose-with-db.yml stop prometheus 2>/dev/null || true
echo -e "${GREEN}✓ Prometheus stopped${NC}"
echo ""

# Step 5: Remove old Prometheus container
echo -e "${YELLOW}[5/7] Removing Prometheus container...${NC}"
docker rm -f prometheus 2>/dev/null || true
echo -e "${GREEN}✓ Prometheus container removed${NC}"
echo ""

# Step 6: Start Prometheus with new config
echo -e "${YELLOW}[6/7] Starting Prometheus with updated configuration...${NC}"
docker-compose -f docker-compose-with-db.yml up -d prometheus
sleep 10
echo -e "${GREEN}✓ Prometheus started${NC}"
echo ""

# Step 7: Verify metrics
echo -e "${YELLOW}[7/7] Verifying metrics collection...${NC}"
echo ""

# Wait for Prometheus to scrape
echo "Waiting 20 seconds for Prometheus to scrape targets..."
sleep 20

echo "Checking Prometheus targets:"
curl -s 'http://localhost:9090/api/v1/targets' 2>/dev/null | jq -r '.data.activeTargets[] | select(.scrapePool == "node-exporter-k3s-nodes") | "\(.labels.instance): \(.health)"' || echo "  No targets found yet"
echo ""

echo "Testing CPU metrics query:"
CPU_RESULT=$(curl -s 'http://localhost:9090/api/v1/query?query=up{job="node-exporter-k3s-nodes"}' 2>/dev/null | jq -r '.data.result | length')
echo "Found $CPU_RESULT K3s nodes being monitored"
echo ""

if [ "$CPU_RESULT" -gt 0 ]; then
    echo "Testing individual node CPU usage:"
    curl -s 'http://localhost:9090/api/v1/query?query=100%20-%20(avg%20by%20(instance)%20(irate(node_cpu_seconds_total{mode=%22idle%22,job=%22node-exporter-k3s-nodes%22}[5m]))%20*%20100)' 2>/dev/null | \
      jq -r '.data.result[] | "\(.metric.instance): \(.value[1] | tonumber | round)%"' || echo "  No data yet"
    echo ""

    echo "Testing individual node memory usage:"
    curl -s 'http://localhost:9090/api/v1/query?query=(1%20-%20(node_memory_MemAvailable_bytes{job=%22node-exporter-k3s-nodes%22}%20/%20node_memory_MemTotal_bytes{job=%22node-exporter-k3s-nodes%22}))%20*%20100' 2>/dev/null | \
      jq -r '.data.result[] | "\(.metric.instance): \(.value[1] | tonumber | round)%"' || echo "  No data yet"
    echo ""
fi

echo "Testing pod counts:"
TOTAL_PODS=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_pod_info)' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
PENDING_PODS=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_pod_status_phase{phase="Pending"})' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
echo "Total Pods: $TOTAL_PODS"
echo "Pending Pods: $PENDING_PODS"
echo ""

echo "Testing node counts:"
TOTAL_NODES=$(curl -s 'http://localhost:9090/api/v1/query?query=count(kube_node_info)' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
READY_NODES=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_node_status_condition{condition="Ready",status="true"})' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
echo "Total Nodes: $TOTAL_NODES"
echo "Ready Nodes: $READY_NODES"
echo ""

echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}✓ Monitoring Stack Setup Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "Access your monitoring tools:"
echo "  • Prometheus: http://localhost:9090"
echo "  • Grafana: http://localhost:3000 (admin/admin)"
echo "  • Grafana Dashboard: http://localhost:3000/d/k3s-cluster-monitoring"
echo ""
echo "Useful commands:"
echo "  • View node-exporter pods: KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl get pods -n kube-system -l app.kubernetes.io/name=node-exporter"
echo "  • Check Prometheus targets: curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.scrapePool == \"node-exporter-k3s-nodes\")'"
echo "  • View Prometheus logs: docker logs prometheus"
echo "  • Run verification: ./verify-metrics.sh"
echo ""

# Cleanup temp kubeconfig
rm -f ./kubeconfig/kubeconfig-host.tmp

echo "Temp kubeconfig cleaned up."