#!/usr/bin/env python3
"""
Metrics collector module for gathering cluster and node metrics
"""

import requests
from typing import Dict, Any, Optional
from kubernetes import client
from kubernetes import config as k8s_config
from database import DatabaseManager
from models.metrics import NodeMetrics, ClusterMetrics, ClusterCapacity
from core.logging_config import get_logger
from kubernetes.client import ApiClient

logger = get_logger(__name__)


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
            logger.info(f"Config: {self.config}")
            kube_config = self.config.get('autoscaler', {}).get('kubernetes', {})
            logger.info(f"Kubernetes config: {kube_config}")

            if kube_config.get('in_cluster'):
                logger.info("Loading in-cluster config")
                k8s_config.load_incluster_config()
            else:
                kubeconfig_path = kube_config.get('kubeconfig_path')
                logger.info(f"Loading kubeconfig from: {kubeconfig_path}")

                # Check if file exists
                import os
                if not os.path.exists(kubeconfig_path):
                    logger.error(f"Kubeconfig file not found at: {kubeconfig_path}")
                    raise FileNotFoundError(f"Kubeconfig file not found: {kubeconfig_path}")

                # Load the kubeconfig
                logger.info("Attempting to load kubeconfig...")
                k8s_config.load_kube_config(config_file=kubeconfig_path)
                logger.info("Kubeconfig loaded successfully")

                # Override the server host if specified in config
                server_host = kube_config.get('server_host')
                if server_host and server_host != 'localhost' and server_host != '127.0.0.1':
                    logger.info(f"Server host override: {server_host}")
                    # Try to get and modify the configuration
                    try:
                        from kubernetes.client import Configuration
                        configuration = Configuration.get_default_copy()

                        # Override the host - replace 127.0.0.1 or localhost with the actual server host
                        if configuration.host and ('127.0.0.1' in configuration.host or 'localhost' in configuration.host):
                            # Extract the port from the current host
                            port = configuration.host.split(':')[-1]
                            # Build new host URL
                            configuration.host = f"https://{server_host}:{port}"
                            logger.info(f"Overriding Kubernetes API server to: {configuration.host}")

                            # Set the modified configuration
                            Configuration.set_default(configuration)
                    except (ImportError, AttributeError):
                        # Fallback for newer versions - use ApiClient
                        api_client = ApiClient()
                        if hasattr(api_client, 'configuration'):
                            configuration = api_client.configuration
                            if configuration.host and ('127.0.0.1' in configuration.host or 'localhost' in configuration.host):
                                port = configuration.host.split(':')[-1]
                                configuration.host = f"https://{server_host}:{port}"
                                logger.info(f"Overriding Kubernetes API server to: {configuration.host}")

            self.k8s_api = client.CoreV1Api()
            logger.info("Kubernetes client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Kubernetes client: {e}", exc_info=True)
            self.k8s_api = None

    def collect(self) -> ClusterMetrics:
        """
        Collect all metrics

        Returns:
            ClusterMetrics object containing collected metrics
        """
        # Initialize with default values
        metrics_dict = {
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
                metrics_dict.update(self._get_kubernetes_metrics())

            # Get detailed metrics from Prometheus
            prometheus_metrics = self._get_prometheus_metrics()
            metrics_dict.update(prometheus_metrics)

            # Convert to Pydantic model
            metrics = ClusterMetrics(**metrics_dict)
            return metrics

        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")
            # Return default metrics on error
            return ClusterMetrics(**metrics_dict)

    def _get_kubernetes_metrics(self) -> Dict[str, Any]:
        """Get metrics from Kubernetes API"""
        metrics = {"current_nodes": 0, "ready_nodes": 0, "pending_pods": 0}

        try:
            logger.info("Fetching nodes from Kubernetes API...")
            # Get all nodes
            nodes = self.k8s_api.list_node()
            logger.info(f"Found {len(nodes.items)} total nodes in Kubernetes")

            # Count worker nodes (exclude control-plane)
            worker_nodes = []
            ready_nodes = 0
            control_plane_nodes = []

            for node in nodes.items:
                node_name = node.metadata.name
                node_labels = node.metadata.labels or {}
                logger.debug(f"Processing node: {node_name}, labels: {node_labels}")

                # Skip control-plane nodes
                if 'node-role.kubernetes.io/control-plane' in node_labels or 'node-role.kubernetes.io/master' in node_labels:
                    control_plane_nodes.append(node_name)
                    logger.debug(f"Skipping control-plane node: {node_name}")
                    continue

                # Skip cordoned nodes (being removed)
                if hasattr(node, 'spec') and node.spec.unschedulable:
                    logger.info(f"Skipping cordoned node: {node_name}")
                    continue

                # Check if node is ready before counting it
                node_ready = False
                if node.status.conditions:
                    for condition in node.status.conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            node_ready = True
                            ready_nodes += 1
                            break
                    if not node_ready:
                        # Node has conditions but is not Ready
                        logger.info(f"Skipping NotReady node: {node_name}")
                        continue
                else:
                    # Node has no conditions, treat as not ready
                    logger.warning(f"Node {node_name} has no conditions, skipping")
                    continue

                worker_nodes.append(node)
                logger.info(f"Found ready worker node: {node_name}")

            logger.info(f"Control-plane nodes: {control_plane_nodes}")
            logger.info(f"Worker nodes: {[n.metadata.name for n in worker_nodes]}")

            metrics["current_nodes"] = len(worker_nodes)
            metrics["ready_nodes"] = ready_nodes

            # Get pending pods
            logger.info("Fetching pending pods...")
            pods = self.k8s_api.list_pod_for_all_namespaces(field_selector="status.phase=Pending")
            metrics["pending_pods"] = len(pods.items)
            logger.info(f"Found {metrics['pending_pods']} pending pods")

            logger.info(f"Kubernetes metrics: nodes={metrics['current_nodes']}, ready={metrics['ready_nodes']}, pending={metrics['pending_pods']}")

        except Exception as e:
            logger.error(f"Error getting Kubernetes metrics: {e}", exc_info=True)

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

            # Get actual memory usage (average across all nodes)
            mem_usage_response = self._query_prometheus(
                'avg((1 - (node_memory_MemAvailable_bytes{job="node-exporter-k3s-nodes"} / node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"})) * 100)'
            )

            # Get CPU requests and allocatable
            cpu_requests_response = self._query_prometheus(
                'sum(kube_pod_container_resource_requests{resource="cpu"})'
            )
            cpu_allocatable_response = self._query_prometheus(
                'sum(kube_node_status_allocatable{resource="cpu"})'
            )

            # Get memory requests and allocatable for allocation tracking
            mem_requests_response = self._query_prometheus(
                'sum(kube_pod_container_resource_requests{resource="memory"})'
            )
            mem_allocatable_response = self._query_prometheus(
                'sum(kube_node_status_allocatable{resource="memory"})'
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

            # Process actual memory usage
            if mem_usage_response and len(mem_usage_response) > 0:
                metrics["avg_memory"] = float(mem_usage_response[0]["value"][1])

            # Store memory allocation info (for reference)
            if mem_requests_response and mem_allocatable_response:
                mem_requested = float(mem_requests_response[0]["value"][1])
                mem_allocatable = float(mem_allocatable_response[0]["value"][1])
                metrics["total_memory"] = mem_requested
                metrics["allocatable_memory"] = mem_allocatable

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
            # Use requests' params which handles URL encoding properly
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

    def get_node_metrics(self) -> Dict[str, NodeMetrics]:
        """Get individual node metrics"""
        node_metrics = {}

        try:
            # Get CPU by node - using the same query as Grafana dashboard
            cpu_by_node = self._query_prometheus(
                '100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle",job="node-exporter-k3s-nodes"}[5m])) * 100)'
            )

            # Get memory usage by node - using the same query as Grafana dashboard
            memory_by_node = self._query_prometheus(
                '(1 - (node_memory_MemAvailable_bytes{job="node-exporter-k3s-nodes"} / node_memory_MemTotal_bytes{job="node-exporter-k3s-nodes"})) * 100'
            )

            # Combine CPU and memory data
            node_data = {}

            # Process CPU metrics
            if cpu_by_node:
                for item in cpu_by_node:
                    instance = item['metric'].get('instance', 'unknown')
                    # Extract node name from instance (e.g., "k3s-master" from "k3s-master:9100")
                    node_name = instance.split(':')[0] if ':' in instance else instance
                    cpu_usage = float(item['value'][1])

                    if node_name not in node_data:
                        node_data[node_name] = {}
                    node_data[node_name]['instance'] = instance
                    node_data[node_name]['cpu_usage'] = cpu_usage

            # Process memory metrics
            if memory_by_node:
                for item in memory_by_node:
                    instance = item['metric'].get('instance', 'unknown')
                    node_name = instance.split(':')[0] if ':' in instance else instance
                    memory_usage = float(item['value'][1])

                    if node_name not in node_data:
                        node_data[node_name] = {}
                    node_data[node_name]['instance'] = instance
                    node_data[node_name]['memory_usage'] = memory_usage

                    # Also get raw memory bytes for more detailed info
                    memory_available = self._query_prometheus(
                        f'node_memory_MemAvailable_bytes{{instance="{instance}",job="node-exporter-k3s-nodes"}}'
                    )
                    memory_total = self._query_prometheus(
                        f'node_memory_MemTotal_bytes{{instance="{instance}",job="node-exporter-k3s-nodes"}}'
                    )

                    if memory_available and memory_total:
                        available_bytes = float(memory_available[0]['value'][1])
                        total_bytes = float(memory_total[0]['value'][1])
                        used_bytes = total_bytes - available_bytes
                        node_data[node_name]['memory_used_bytes'] = used_bytes
                        node_data[node_name]['memory_total_bytes'] = total_bytes
                        node_data[node_name]['memory_available_bytes'] = available_bytes

            # Create NodeMetrics objects
            for node_name, data in node_data.items():
                node_metrics[node_name] = NodeMetrics(
                    node_name=node_name,
                    instance=data.get('instance', 'unknown'),
                    cpu_usage=data.get('cpu_usage', 0.0),
                    memory_usage=data.get('memory_usage', 0.0),
                    memory_used_bytes=data.get('memory_used_bytes'),
                    memory_total_bytes=data.get('memory_total_bytes'),
                    memory_available_bytes=data.get('memory_available_bytes')
                )

            logger.info(f"Collected metrics for {len(node_metrics)} nodes")

        except Exception as e:
            logger.error(f"Error getting node metrics: {e}")

        return node_metrics

    def get_cluster_capacity(self) -> ClusterCapacity:
        """Get cluster resource capacity"""
        cpu_cores = 0
        memory_bytes = 0
        pods_capacity = 0

        try:
            if self.k8s_api:
                nodes = self.k8s_api.list_node()

                for node in nodes.items:
                    # Skip control-plane
                    if node.metadata.labels and 'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                        continue

                    if node.status.allocatable:
                        cpu_cores += float(node.status.allocatable.get("cpu", 0))
                        memory_bytes += self._parse_memory(node.status.allocatable.get("memory", "0"))
                        pods_capacity += int(node.status.allocatable.get("pods", 0))

                # Convert memory to GB
                memory_gb = memory_bytes / (1024**3)

                return ClusterCapacity(
                    cpu_cores=cpu_cores,
                    memory_bytes=memory_bytes,
                    memory_gb=memory_gb,
                    pods_capacity=pods_capacity
                )

        except Exception as e:
            logger.error(f"Error getting cluster capacity: {e}")

        return ClusterCapacity(
            cpu_cores=0,
            memory_bytes=0,
            memory_gb=0,
            pods_capacity=0
        )

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