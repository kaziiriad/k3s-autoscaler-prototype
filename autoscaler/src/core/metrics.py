#!/usr/bin/env python3
"""
Metrics collector module for gathering cluster and node metrics
"""

import logging
import requests
from typing import Dict, Any, Optional
from kubernetes import client, config as k8s_config
from database import DatabaseManager

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Collects metrics from Prometheus and Kubernetes API"""

    def __init__(self, config: Dict, database: DatabaseManager):
        """
        Initialize metrics collector

        Args:
            config: Autoscaler configuration
            database: Database manager instance
        """
        self.config = config
        self.database = database
        self.prometheus_url = "http://prometheus:9090"

        # Initialize Kubernetes client
        self._init_kubernetes_client()

    def _init_kubernetes_client(self):
        """Initialize Kubernetes API client"""
        try:
            if self.config['autoscaler']['kubernetes']['in_cluster']:
                k8s_config.load_incluster_config()
            else:
                kubeconfig_path = self.config['autoscaler']['kubernetes']['kubeconfig_path']
                
                # Load the kubeconfig
                k8s_config.load_kube_config(config_file=kubeconfig_path)
                
                # Override the server host if specified in config
                server_host = self.config['autoscaler']['kubernetes'].get('server_host')
                if server_host and server_host != 'localhost' and server_host != '127.0.0.1':
                    # Get the configuration
                    configuration = k8s_config.Configuration.get_default_copy()
                    
                    # Override the host - replace 127.0.0.1 or localhost with the actual server host
                    if '127.0.0.1' in configuration.host or 'localhost' in configuration.host:
                        # Extract the port from the current host
                        port = configuration.host.split(':')[-1]
                        # Build new host URL
                        configuration.host = f"https://{server_host}:{port}"
                        logger.info(f"Overriding Kubernetes API server to: {configuration.host}")
                        
                        # Set the modified configuration
                        k8s_config.Configuration.set_default(configuration)
                
            self.k8s_api = client.CoreV1Api()
            logger.info("Kubernetes client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            self.k8s_api = None

    def collect(self) -> Dict[str, Any]:
        """
        Collect all metrics

        Returns:
            Dict containing collected metrics
        """
        metrics = {
            "current_nodes": 0,
            "ready_nodes": 0,
            "pending_pods": 0,
            "avg_cpu": 0.0,
            "avg_memory": 0.0,
            "total_cpu": 0.0,
            "total_memory": 0.0,
            "allocatable_cpu": 0.0,
            "allocatable_memory": 0.0
        }

        try:
            # Get node count from Kubernetes API
            if self.k8s_api:
                metrics.update(self._get_kubernetes_metrics())

            # Get detailed metrics from Prometheus
            prometheus_metrics = self._get_prometheus_metrics()
            metrics.update(prometheus_metrics)

            # Cache metrics in database
            self.database.cache_metrics(metrics)

            return metrics

        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
            # Return cached metrics if available
            cached = self.database.get_cached_metrics()
            return cached or metrics

    def _get_kubernetes_metrics(self) -> Dict[str, Any]:
        """Get metrics from Kubernetes API"""
        metrics = {}

        try:
            # Get all nodes
            nodes = self.k8s_api.list_node()

            # Count worker nodes (exclude control-plane)
            worker_nodes = []
            ready_nodes = 0

            for node in nodes.items:
                # Skip control-plane nodes
                if node.metadata.labels and 'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                    continue

                worker_nodes.append(node)

                # Check if node is ready
                if node.status.conditions:
                    for condition in node.status.conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            ready_nodes += 1
                            break

            metrics["current_nodes"] = len(worker_nodes)
            metrics["ready_nodes"] = ready_nodes

            # Get pending pods
            pods = self.k8s_api.list_pod_for_all_namespaces(field_selector="status.phase=Pending")
            metrics["pending_pods"] = len(pods.items)

            logger.debug(f"Kubernetes metrics: {metrics}")

        except Exception as e:
            logger.error(f"Error getting Kubernetes metrics: {e}")

        return metrics

    def _get_prometheus_metrics(self) -> Dict[str, Any]:
        """Get metrics from Prometheus"""
        metrics = {
            "avg_cpu": 0.0,
            "avg_memory": 0.0,
            "total_cpu": 0.0,
            "total_memory": 0.0,
            "allocatable_cpu": 0.0,
            "allocatable_memory": 0.0
        }

        try:
            # Get CPU usage
            cpu_response = self._query_prometheus(
                'avg(100 - (irate(node_cpu_seconds_total{mode="idle",job=~"node-exporter-k3s-nodes"}[5m])) * 100)'
            )

            # Get memory usage using kube-state-metrics
            mem_requests_response = self._query_prometheus(
                'sum(kube_pod_container_resource_requests{resource="memory"})'
            )
            mem_allocatable_response = self._query_prometheus(
                'sum(kube_node_status_allocatable{resource="memory"})'
            )

            # Get CPU requests and allocatable
            cpu_requests_response = self._query_prometheus(
                'sum(kube_pod_container_resource_requests{resource="cpu"})'
            )
            cpu_allocatable_response = self._query_prometheus(
                'sum(kube_node_status_allocatable{resource="cpu"})'
            )

            # Process CPU metrics
            if cpu_response and len(cpu_response) > 0:
                metrics["avg_cpu"] = float(cpu_response[0]["value"][1])

            if cpu_requests_response and cpu_allocatable_response:
                cpu_requested = float(cpu_requests_response[0]["value"][1])
                cpu_allocatable = float(cpu_allocatable_response[0]["value"][1])
                metrics["total_cpu"] = cpu_requested
                metrics["allocatable_cpu"] = cpu_allocatable
                if cpu_allocatable > 0:
                    metrics["avg_cpu"] = max(metrics["avg_cpu"], (cpu_requested / cpu_allocatable) * 100)

            # Process memory metrics
            if mem_requests_response and mem_allocatable_response:
                mem_requested = float(mem_requests_response[0]["value"][1])
                mem_allocatable = float(mem_allocatable_response[0]["value"][1])
                metrics["total_memory"] = mem_requested
                metrics["allocatable_memory"] = mem_allocatable
                if mem_allocatable > 0:
                    metrics["avg_memory"] = (mem_requested / mem_allocatable) * 100

            # Convert bytes to GB for readability
            if metrics["total_memory"] > 0:
                metrics["total_memory_gb"] = metrics["total_memory"] / (1024**3)
                metrics["allocatable_memory_gb"] = metrics["allocatable_memory"] / (1024**3)

            logger.debug(f"Prometheus metrics: cpu={metrics['avg_cpu']:.1f}%, memory={metrics['avg_memory']:.1f}%")

        except Exception as e:
            logger.error(f"Error getting Prometheus metrics: {e}")

        return metrics

    def _query_prometheus(self, query: str) -> Optional[list]:
        """Query Prometheus API"""
        try:
            response = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={'query': query},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data['status'] == 'success':
                return data['data']['result']
            else:
                logger.error(f"Prometheus query failed: {data.get('error', 'Unknown error')}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying Prometheus: {e}")
            return None
        except Exception as e:
            logger.error(f"Error processing Prometheus response: {e}")
            return None

    def get_node_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get individual node metrics"""
        node_metrics = {}

        try:
            # Get CPU by node
            cpu_by_node = self._query_prometheus(
                'avg by (node) (100 - (irate(node_cpu_seconds_total{mode="idle",job=~"node-exporter-k3s-nodes"}[5m])) * 100)'
            )

            # Get memory by node
            memory_by_node = self._query_prometheus(
                'sum by (node) (kube_pod_container_resource_requests{resource="memory"}) / sum by (node) (kube_node_status_allocatable{resource="memory"}) * 100'
            )

            # Process CPU metrics
            if cpu_by_node:
                for item in cpu_by_node:
                    node = item['metric'].get('node', 'unknown')
                    cpu_usage = float(item['value'][1])

                    if node not in node_metrics:
                        node_metrics[node] = {}
                    node_metrics[node]['cpu_usage'] = cpu_usage

            # Process memory metrics
            if memory_by_node:
                for item in memory_by_node:
                    node = item['metric'].get('node', 'unknown')
                    memory_usage = float(item['value'][1])

                    if node not in node_metrics:
                        node_metrics[node] = {}
                    node_metrics[node]['memory_usage'] = memory_usage

        except Exception as e:
            logger.error(f"Error getting node metrics: {e}")

        return node_metrics

    def get_cluster_capacity(self) -> Dict[str, Any]:
        """Get cluster resource capacity"""
        capacity = {
            "cpu_cores": 0,
            "memory_bytes": 0,
            "pods_capacity": 0
        }

        try:
            if self.k8s_api:
                nodes = self.k8s_api.list_node()

                for node in nodes.items:
                    # Skip control-plane
                    if node.metadata.labels and 'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                        continue

                    if node.status.allocatable:
                        capacity["cpu_cores"] += float(node.status.allocatable.get("cpu", 0))
                        capacity["memory_bytes"] += self._parse_memory(node.status.allocatable.get("memory", "0"))
                        capacity["pods_capacity"] += int(node.status.allocatable.get("pods", 0))

                # Convert memory to GB
                capacity["memory_gb"] = capacity["memory_bytes"] / (1024**3)

        except Exception as e:
            logger.error(f"Error getting cluster capacity: {e}")

        return capacity

    def _parse_memory(self, memory_str: str) -> int:
        """Parse memory string to bytes"""
        try:
            memory_str = memory_str.strip()
            if memory_str.endswith('Ki'):
                return int(memory_str[:-2]) * 1024
            elif memory_str.endswith('Mi'):
                return int(memory_str[:-2]) * 1024**2
            elif memory_str.endswith('Gi'):
                return int(memory_str[:-2]) * 1024**3
            elif memory_str.endswith('K'):
                return int(memory_str[:-1]) * 1000
            elif memory_str.endswith('M'):
                return int(memory_str[:-1]) * 1000**2
            elif memory_str.endswith('G'):
                return int(memory_str[:-1]) * 1000**3
            else:
                return int(memory_str)
        except:
            return 0