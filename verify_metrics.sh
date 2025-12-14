#!/bin/bash

echo "========================================="
echo "K3s Monitoring Stack Verification"
echo "========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

check_mark="${GREEN}✓${NC}"
cross_mark="${RED}✗${NC}"

# Create temp kubeconfig for host
cp ./kubeconfig/kubeconfig ./kubeconfig/kubeconfig-host.tmp 2>/dev/null || true
sed -i 's|https://k3s-master:6443|https://127.0.0.1:6443|g' ./kubeconfig/kubeconfig-host.tmp 2>/dev/null || true

echo "1. Checking node-exporter pods..."
POD_COUNT=$(kubectl --kubeconfig=./kubeconfig/kubeconfig-host.tmp get pods -n kube-system -l app.kubernetes.io/name=node-exporter --no-headers 2>/dev/null | wc -l)
READY_COUNT=$(kubectl --kubeconfig=./kubeconfig/kubeconfig-host.tmp get pods -n kube-system -l app.kubernetes.io/name=node-exporter --no-headers 2>/dev/null | grep "Running" | wc -l)
if [ "$POD_COUNT" -gt 0 ] && [ "$POD_COUNT" -eq "$READY_COUNT" ]; then
    echo -e "   $check_mark $POD_COUNT node-exporter pods running"
else
    echo -e "   $cross_mark Only $READY_COUNT of $POD_COUNT pods ready"
fi
echo ""

echo "2. Checking Prometheus targets..."
TARGET_COUNT=$(curl -s 'http://localhost:9090/api/v1/targets' 2>/dev/null | jq -r '.data.activeTargets[] | select(.scrapePool == "node-exporter-k3s-nodes") | .health' | grep -c "up" || echo "0")
if [ "$TARGET_COUNT" -gt 0 ]; then
    echo -e "   $check_mark $TARGET_COUNT K3s nodes being scraped"
    curl -s 'http://localhost:9090/api/v1/targets' 2>/dev/null | jq -r '.data.activeTargets[] | select(.scrapePool == "node-exporter-k3s-nodes") | "     - \(.labels.instance): \(.health)"'
else
    echo -e "   $cross_mark No K3s nodes being scraped"
fi
echo ""

echo "3. Testing individual node CPU metrics..."
CPU_METRICS=$(curl -s 'http://localhost:9090/api/v1/query?query=node_cpu_seconds_total{job="node-exporter-k3s-nodes"}' 2>/dev/null | jq -r '.data.result | length')
if [ "$CPU_METRICS" -gt 0 ]; then
    echo -e "   $check_mark CPU metrics available for $CPU_METRICS instances"
    echo "   Current CPU usage by node:"
    curl -s 'http://localhost:9090/api/v1/query?query=100%20-%20(avg%20by%20(instance)%20(irate(node_cpu_seconds_total{mode=%22idle%22,job=%22node-exporter-k3s-nodes%22}[5m]))%20*%20100)' 2>/dev/null | \
      jq -r '.data.result[] | "     - \(.metric.instance): \(.value[1] | tonumber | round)%"'
else
    echo -e "   $cross_mark No CPU metrics found"
fi
echo ""

echo "4. Testing individual node memory metrics..."
MEM_METRICS=$(curl -s 'http://localhost:9090/api/v1/query?query=node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"}' 2>/dev/null | jq -r '.data.result | length')
if [ "$MEM_METRICS" -gt 0 ]; then
    echo -e "   $check_mark Memory metrics available for $MEM_METRICS instances"
    echo "   Current memory usage by node:"
    curl -s 'http://localhost:9090/api/v1/query?query=(1%20-%20(node_memory_MemAvailable_bytes{job=%22node-exporter-k3s-nodes%22}%20/%20node_memory_MemTotal_bytes{job=%22node-exporter-k3s-nodes%22}))%20*%20100' 2>/dev/null | \
      jq -r '.data.result[] | "     - \(.metric.instance): \(.value[1] | tonumber | round)%"'
else
    echo -e "   $cross_mark No memory metrics found"
fi
echo ""

echo "5. Testing collective average metrics..."
AVG_CPU=$(curl -s 'http://localhost:9090/api/v1/query?query=avg(100%20-%20(irate(node_cpu_seconds_total{mode=%22idle%22,job=%22node-exporter-k3s-nodes%22}[5m]))%20*%20100)' 2>/dev/null | jq -r '.data.result[0].value[1] // "N/A"')
AVG_MEM=$(curl -s 'http://localhost:9090/api/v1/query?query=avg((1%20-%20(node_memory_MemAvailable_bytes{job=%22node-exporter-k3s-nodes%22}%20/%20node_memory_MemTotal_bytes{job=%22node-exporter-k3s-nodes%22}))%20*%20100)' 2>/dev/null | jq -r '.data.result[0].value[1] // "N/A"')
if [ "$AVG_CPU" != "N/A" ]; then
    echo -e "   $check_mark Average CPU: $(echo $AVG_CPU | awk '{printf "%.1f", $1}')%"
else
    echo -e "   $cross_mark Average CPU: Not available"
fi
if [ "$AVG_MEM" != "N/A" ]; then
    echo -e "   $check_mark Average Memory: $(echo $AVG_MEM | awk '{printf "%.1f", $1}')%"
else
    echo -e "   $cross_mark Average Memory: Not available"
fi
echo ""

echo "6. Testing Kubernetes metrics..."
TOTAL_NODES=$(curl -s 'http://localhost:9090/api/v1/query?query=count(kube_node_info)' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
READY_NODES=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_node_status_condition{condition="Ready",status="true"})' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
TOTAL_PODS=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_pod_info)' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')
PENDING_PODS=$(curl -s 'http://localhost:9090/api/v1/query?query=sum(kube_pod_status_phase{phase="Pending"})' 2>/dev/null | jq -r '.data.result[0].value[1] // "0"')

if [ "$TOTAL_NODES" != "0" ]; then
    echo -e "   $check_mark Total Nodes: $TOTAL_NODES"
    echo -e "   $check_mark Ready Nodes: $READY_NODES"
else
    echo -e "   $cross_mark Node metrics not available"
fi

if [ "$TOTAL_PODS" != "0" ]; then
    echo -e "   $check_mark Total Pods: $TOTAL_PODS"
    echo -e "   $check_mark Pending Pods: $PENDING_PODS"
else
    echo -e "   $cross_mark Pod metrics not available"
fi
echo ""

echo "7. Testing Grafana dashboard..."
GRAFANA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000 2>/dev/null)
if [ "$GRAFANA_STATUS" == "200" ]; then
    echo -e "   $check_mark Grafana is accessible at http://localhost:3000"
    echo "   → Dashboard: http://localhost:3000/d/k3s-cluster-monitoring"
else
    echo -e "   $cross_mark Grafana is not accessible (HTTP $GRAFANA_STATUS)"
fi
echo ""

echo "========================================="
echo "Verification Complete"
echo "========================================="
echo ""
echo "If all checks passed, your monitoring stack is ready!"
echo "If any checks failed, see the troubleshooting guide below."
echo ""

# Cleanup
rm -f ./kubeconfig/kubeconfig-host.tmp