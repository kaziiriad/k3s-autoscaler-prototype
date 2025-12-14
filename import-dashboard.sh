#!/bin/bash

# Import K3s Cluster Dashboard to Grafana
# Usage: ./import-dashboard.sh

GRAFANA_URL="http://localhost:3000"
GRAFANA_USER="admin"
GRAFANA_PASSWORD="admin"  # Default from docker-compose

# Create Prometheus datasource if it doesn't exist
curl -X POST \
  "${GRAFANA_URL}/api/datasources" \
  -H "Content-Type: application/json" \
  -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  -d '{
    "name": "Prometheus",
    "type": "prometheus",
    "url": "http://prometheus:9090",
    "access": "proxy",
    "isDefault": true
  }' | jq '.'

echo ""
echo "Importing K3s Cluster Dashboard..."

# Import dashboard
curl -X POST \
  "${GRAFANA_URL}/api/dashboards/db" \
  -H "Content-Type: application/json" \
  -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  -d '{
    "dashboard": {
      "id": null,
      "title": "K3s Cluster Overview",
      "tags": ["k3s", "kubernetes", "cluster"],
      "timezone": "browser",
      "panels": [
        {
          "id": 1,
          "title": "Running Nodes",
          "type": "stat",
          "targets": [
            {
              "expr": "count(kube_node_info{node=~\"k3s-worker.*\"})",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 6, "x": 0, "y": 0},
          "fieldConfig": {
            "defaults": {
              "unit": "short"
            }
          }
        },
        {
          "id": 2,
          "title": "Ready Nodes",
          "type": "stat",
          "targets": [
            {
              "expr": "sum(kube_node_status_condition{condition=\"Ready\",status=\"true\",node=~\"k3s-worker.*\"})",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 6, "x": 6, "y": 0},
          "fieldConfig": {
            "defaults": {
              "unit": "short"
            }
          }
        },
        {
          "id": 3,
          "title": "Average CPU Usage",
          "type": "stat",
          "targets": [
            {
              "expr": "avg(100 - (irate(node_cpu_seconds_total{mode=\"idle\",job=\"node-exporter-host\"}[5m])) * 100)",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 6, "x": 12, "y": 0},
          "fieldConfig": {
            "defaults": {
              "unit": "percent",
              "thresholds": {
                "mode": "absolute",
                "steps": [
                  {"color": "green", "value": null},
                  {"color": "yellow", "value": 60},
                  {"color": "red", "value": 80}
                ]
              }
            }
          }
        },
        {
          "id": 4,
          "title": "Average Memory Usage",
          "type": "stat",
          "targets": [
            {
              "expr": "(1 - (node_memory_MemAvailable_bytes{job=\"node-exporter-host\"} / node_memory_MemTotal_bytes{job=\"node-exporter-host\"})) * 100",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 6, "x": 18, "y": 0},
          "fieldConfig": {
            "defaults": {
              "unit": "percent",
              "thresholds": {
                "mode": "absolute",
                "steps": [
                  {"color": "green", "value": null},
                  {"color": "yellow", "value": 70},
                  {"color": "red", "value": 90}
                ]
              }
            }
          }
        },
        {
          "id": 5,
          "title": "Pod Count by Phase",
          "type": "piechart",
          "targets": [
            {
              "expr": "sum by (phase) (kube_pod_status_phase)",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
          "fieldConfig": {
            "defaults": {
              "unit": "short"
            }
          }
        },
        {
          "id": 6,
          "title": "Pending Pods",
          "type": "stat",
          "targets": [
            {
              "expr": "sum(kube_pod_status_phase{phase=\"Pending\"})",
              "refId": "A"
            }
          ],
          "gridPos": {"h": 8, "w": 6, "x": 12, "y": 8},
          "fieldConfig": {
            "defaults": {
              "unit": "short"
            }
          }
        },
        {
          "id": 7,
          "title": "Node CPU Usage",
          "type": "timeseries",
          "targets": [
            {
              "expr": "100 - (irate(node_cpu_seconds_total{mode=\"idle\",job=\"node-exporter-host\"}[5m])) * 100",
              "legendFormat": "{{instance}}"
            }
          ],
          "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
          "fieldConfig": {
            "defaults": {
              "unit": "percent"
            }
          }
        },
        {
          "id": 8,
          "title": "Node Memory Usage",
          "type": "timeseries",
          "targets": [
            {
              "expr": "(1 - (node_memory_MemAvailable_bytes{job=\"node-exporter-host\"} / node_memory_MemTotal_bytes{job=\"node-exporter-host\"})) * 100",
              "legendFormat": "{{instance}}"
            }
          ],
          "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
          "fieldConfig": {
            "defaults": {
              "unit": "percent"
            }
          }
        }
      ],
      "time": {"from": "now-1h", "to": "now"},
      "refresh": "5s",
      "schemaVersion": 35
    },
    "overwrite": true
  }' | jq '.'

echo ""
echo "Dashboard imported successfully!"
echo "Access it at: ${GRAFANA_URL}/d/k3s-cluster/k3s-cluster-overview"