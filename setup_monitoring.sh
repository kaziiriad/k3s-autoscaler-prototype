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
echo -e "${YELLOW}[1/8] Deploying node-exporter DaemonSet to K3s...${NC}"
if [ -f "monitoring/node-exporter-daemonset.yaml" ]; then
    KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl apply -f monitoring/node-exporter-daemonset.yaml --validate=false
    echo -e "${GREEN}✓ Node-exporter DaemonSet deployed from file${NC}"
else
    echo -e "${YELLOW}⚠ node-exporter-daemonset.yaml not found, creating inline...${NC}"
    KUBECONFIG=./kubeconfig/kubeconfig-host.tmp cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: node-exporter
  namespace: kube-system
  labels:
    app.kubernetes.io/name: node-exporter
    app.kubernetes.io/version: 1.6.1
    app.kubernetes.io/component: monitoring
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: node-exporter
  template:
    metadata:
      labels:
        app.kubernetes.io/name: node-exporter
        app.kubernetes.io/version: 1.6.1
        app.kubernetes.io/component: monitoring
    spec:
      serviceAccountName: node-exporter
      containers:
      - name: node-exporter
        image: quay.io/prometheus/node-exporter:v1.6.1
        args:
          - --path.rootfs=/host
        ports:
          - containerPort: 9100
            hostPort: 9100
            name: metrics
            protocol: TCP
        resources:
          limits:
            cpu: 250m
            memory: 180Mi
          requests:
            cpu: 102m
            memory: 180Mi
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            add:
            - NET_RAW
            drop:
            - ALL
          readOnlyRootFilesystem: true
          runAsGroup: 65532
          runAsNonRoot: true
          runAsUser: 65532
        volumeMounts:
        - mountPath: /host
          mountPropagation: HostToContainer
          name: rootfs
          readOnly: true
      hostNetwork: true
      hostPID: true
      nodeSelector:
        kubernetes.io/os: linux
      tolerations:
      - operator: Exists
      volumes:
      - hostPath:
          path: /
          type: Directory
        name: rootfs
EOF
    echo -e "${GREEN}✓ Node-exporter DaemonSet deployed inline${NC}"
fi
echo ""

# Step 2: Deploy node-exporter Service
echo -e "${YELLOW}[2/8] Deploying node-exporter Service...${NC}"
if [ -f "monitoring/node-exporter-service.yaml" ]; then
    KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl apply -f monitoring/node-exporter-service.yaml
    echo -e "${GREEN}✓ Node-exporter Service deployed from file${NC}"
else
    echo -e "${YELLOW}⚠ node-exporter-service.yaml not found, creating inline...${NC}"
    KUBECONFIG=./kubeconfig/kubeconfig-host.tmp cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: node-exporter
  namespace: kube-system
  labels:
    app.kubernetes.io/name: node-exporter
    app.kubernetes.io/component: monitoring
spec:
  type: ClusterIP
  ports:
    - name: metrics
      port: 9100
      targetPort: 9100
      protocol: TCP
  selector:
    app.kubernetes.io/name: node-exporter
EOF
    echo -e "${GREEN}✓ Node-exporter Service deployed inline${NC}"
fi
echo ""

# Step 3: Create ServiceAccount and RBAC for node-exporter
echo -e "${YELLOW}[3/8] Creating ServiceAccount and RBAC...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: node-exporter
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: node-exporter
rules:
- nonResourceURLs: ["/metrics"]
  verbs: ["get"]
- apiGroups: [""]
  resources:
    - nodes/metrics
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: node-exporter
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: node-exporter
subjects:
- kind: ServiceAccount
  name: node-exporter
  namespace: kube-system
EOF
echo -e "${GREEN}✓ ServiceAccount and RBAC created${NC}"
echo ""

# Step 4: Wait for node-exporter pods
echo -e "${YELLOW}[4/8] Waiting for node-exporter pods to be ready...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=node-exporter \
  -n kube-system \
  --timeout=120s
echo -e "${GREEN}✓ Node-exporter pods are ready${NC}"
echo ""

# Step 5: Check pod status
echo -e "${YELLOW}[5/8] Checking node-exporter pod status...${NC}"
KUBECONFIG=./kubeconfig/kubeconfig-host.tmp kubectl get pods -n kube-system -l app.kubernetes.io/name=node-exporter
echo ""

# Step 6: Stop Prometheus to fix config
echo -e "${YELLOW}[6/8] Stopping Prometheus to update configuration...${NC}"
docker-compose -f docker-compose-with-db.yml stop prometheus 2>/dev/null || true
echo -e "${GREEN}✓ Prometheus stopped${NC}"
echo ""

# Step 7: Remove old Prometheus container
echo -e "${YELLOW}[7/8] Removing Prometheus container...${NC}"
docker rm -f prometheus 2>/dev/null || true
echo -e "${GREEN}✓ Prometheus container removed${NC}"
echo ""

# Step 8: Start Prometheus with new config and verify metrics
echo -e "${YELLOW}[8/8] Starting Prometheus and verifying metrics collection...${NC}"
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