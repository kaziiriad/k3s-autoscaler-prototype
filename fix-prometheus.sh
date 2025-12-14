#!/bin/bash

echo "========================================="
echo "Fixing Prometheus Configuration"
echo "========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Step 1: Check kubeconfig exists
echo -e "${YELLOW}[1/5] Checking kubeconfig...${NC}"
if [ ! -f "./kubeconfig/kubeconfig" ]; then
    echo -e "${RED}✗ kubeconfig not found!${NC}"
    echo "Please ensure K3s is running and kubeconfig is generated."
    exit 1
fi
echo -e "${GREEN}✓ Kubeconfig found${NC}"
echo ""

# Step 2: Fix kubeconfig server address
echo -e "${YELLOW}[2/5] Fixing kubeconfig server address...${NC}"
if grep -q "127.0.0.1" ./kubeconfig/kubeconfig; then
    sed -i.bak 's|https://127.0.0.1:6443|https://k3s-master:6443|g' ./kubeconfig/kubeconfig
    echo -e "${GREEN}✓ Server address updated to k3s-master:6443${NC}"
else
    echo -e "${GREEN}✓ Server address is already correct${NC}"
fi
echo ""

# Step 3: Stop Prometheus
echo -e "${YELLOW}[3/5] Stopping Prometheus...${NC}"
docker-compose -f docker-compose-with-db.yml stop prometheus
docker rm -f prometheus 2>/dev/null || true
echo -e "${GREEN}✓ Prometheus stopped and removed${NC}"
echo ""

# Step 4: Verify kubeconfig is readable
echo -e "${YELLOW}[4/5] Verifying kubeconfig file...${NC}"
if [ -r "./kubeconfig/kubeconfig" ]; then
    echo -e "${GREEN}✓ Kubeconfig is readable${NC}"
    echo "  Server: $(grep 'server:' ./kubeconfig/kubeconfig | awk '{print $2}')"
else
    echo -e "${RED}✗ Kubeconfig is not readable${NC}"
    exit 1
fi
echo ""

# Step 5: Start Prometheus
echo -e "${YELLOW}[5/5] Starting Prometheus...${NC}"
docker-compose -f docker-compose-with-db.yml up -d prometheus

# Wait for Prometheus to start
echo "Waiting for Prometheus to start..."
sleep 5

# Check if Prometheus is running
if docker ps | grep -q prometheus; then
    echo -e "${GREEN}✓ Prometheus is running${NC}"
    echo ""
    echo "Checking Prometheus status:"
    curl -s http://localhost:9090/-/healthy && echo -e "${GREEN}✓ Prometheus is healthy${NC}" || echo -e "${RED}✗ Prometheus is not healthy${NC}"
else
    echo -e "${RED}✗ Prometheus failed to start${NC}"
    echo "Check logs with: docker logs prometheus"
    exit 1
fi

echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}✓ Prometheus Fix Complete!${NC}"
echo -e "${GREEN}=========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Check Prometheus: http://localhost:9090"
echo "  2. Check targets: http://localhost:9090/targets"
echo "  3. Run monitoring setup: ./setup_monitoring.sh"
echo ""
