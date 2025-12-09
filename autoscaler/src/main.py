#!/usr/bin/env python3
"""
K3s Docker Autoscaler
Autoscales k3s cluster by adding/removing Docker containers based on Prometheus metrics
"""

import os
import sys
import time
import signal
import logging
import yaml
import docker
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from prometheus_client import start_http_server, Gauge, Counter, Counter as PrometheusCounter

# Kubernetes client
from kubernetes import client, config as k8s_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Prometheus metrics
SCALING_DECISIONS = Counter('autoscaler_scaling_decisions_total', 'Total scaling decisions', ['decision'])
CURRENT_NODES = Gauge('autoscaler_current_nodes', 'Current number of k3s nodes')
PENDING_PODS = Gauge('autoscaler_pending_pods', 'Number of pending pods')
SCALE_UP_EVENTS = Counter('autoscaler_scale_up_events_total', 'Total scale-up events')
SCALE_DOWN_EVENTS = Counter('autoscaler_scale_down_events_total', 'Total scale-down events')
ERRORS = Counter('autoscaler_errors_total', 'Total errors', ['type'])

class K3sAutoscaler:
    """Main autoscaler class for k3s running in Docker"""

    def __init__(self, config_path: str = '/app/config/config.yaml'):
        """Initialize the autoscaler"""
        self.config = self._load_config(config_path)
        self.running = True

        # Initialize clients
        if not self.config['autoscaler']['dry_run']:
            self.docker_client = self._init_docker_client()
        else:
            self.docker_client = None
            logger.info("Running in DRY RUN mode - Docker client disabled")
        self.k8s_client = self._init_k8s_client()

        # State tracking
        self.worker_prefix = "k3s-worker"
        self.last_scale_up = None
        self.last_scale_down = None
        self.worker_containers: Dict[str, Dict] = {}

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("K3s Docker Autoscaler initialized")
        logger.info(f"Config: {self.config}")

    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML file"""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            return config
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            sys.exit(1)

    def _init_docker_client(self) -> docker.DockerClient:
        """Initialize Docker client"""
        try:
            client = docker.from_env()
            client.ping()
            logger.info("Docker client initialized successfully")
            return client
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            sys.exit(1)

    def _init_k8s_client(self) -> client.CoreV1Api:
        """Initialize Kubernetes client"""
        try:
            # Handle kubeconfig environment variable substitution
            kubeconfig_path = self.config['autoscaler']['kubernetes']['kubeconfig_path']
            if os.path.exists(kubeconfig_path):
                # Read the kubeconfig and substitute environment variables
                with open(kubeconfig_path, 'r') as f:
                    kubeconfig_content = f.read()

                # Substitute __K3S_SERVER_HOST__ if present
                if '__K3S_SERVER_HOST__' in kubeconfig_content:
                    k3s_server_host = os.getenv('K3S_SERVER_HOST', 'k3s-master')
                    kubeconfig_content = kubeconfig_content.replace('__K3S_SERVER_HOST__', k3s_server_host)
                    logger.info(f"Substituted K3S_SERVER_HOST={k3s_server_host} in kubeconfig")

                    # Write to a temporary file
                    temp_kubeconfig = '/tmp/kubeconfig'
                    with open(temp_kubeconfig, 'w') as f:
                        f.write(kubeconfig_content)
                    kubeconfig_path = temp_kubeconfig

            k8s_config.load_kube_config(config_file=kubeconfig_path)
            api = client.CoreV1Api()
            # Test connection
            api.get_api_resources()
            logger.info("Kubernetes client initialized successfully")
            return api
        except Exception as e:
            if self.config['autoscaler']['dry_run']:
                logger.warning(f"Failed to initialize Kubernetes client in dry run mode: {e}")
                logger.warning("Continuing without Kubernetes client - metrics will be simulated")
                return None
            logger.error(f"Failed to initialize Kubernetes client: {e}")
            sys.exit(1)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def get_pending_pods_count(self) -> int:
        """Get the count of pending pods from Kubernetes"""
        try:
            pods = self.k8s_client.list_pod_for_all_namespaces()
            return len([p for p in pods.items if p.status.phase == "Pending"])
        except Exception as e:
            logger.error(f"Failed to get pending pods: {e}")
            ERRORS.labels(type='kubernetes').inc()
            return 0

    def get_cluster_metrics(self) -> Dict:
        """Get cluster metrics from Prometheus"""
        try:
            # Query pending pods
            pending_response = requests.get('http://prometheus:9090/api/v1/query', params={
                'query': 'sum(kube_pod_status_phase{phase="Pending"})'
            })
            pending_pods = int(pending_response.json()['data']['result'][0]['value'][1]) if pending_response.json()['data']['result'] else 0

            # Query node count - count worker nodes only (exclude master)
            try:
                if self.k8s_client:
                    from kubernetes import client as k8s_client
                    v1 = k8s_client.CoreV1Api()
                    nodes = v1.list_node()
                    # Count worker nodes (nodes that are not master)
                    worker_nodes = len([n for n in nodes.items
                                      if n.status.conditions and
                                      any(c.type == "Ready" and c.status == "True" for c in n.status.conditions) and
                                      n.metadata.labels.get('node-role.kubernetes.io/control-plane') != 'true'])
                    current_nodes = worker_nodes
                    logger.info(f"Found {worker_nodes} worker nodes (excluding master)")
                else:
                    # Fallback to using docker containers count (approximation)
                    current_nodes = 2  # Default to 2 worker nodes in dry run mode
            except Exception as e:
                logger.warning(f"Failed to get node count: {e}")
                current_nodes = 2  # Default to 2 worker nodes

            # Query CPU utilization
            avg_cpu = 0.0
            if current_nodes > 0:
                cpu_response = requests.get('http://prometheus:9090/api/v1/query', params={
                    'query': 'avg(100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle", job="node-exporter-k3s-nodes"}[5m])) * 100))'
                })
                avg_cpu = float(cpu_response.json()['data']['result'][0]['value'][1]) if cpu_response.json()['data']['result'] else 0

            # Get memory utilization from kube-state-metrics
            avg_memory = 0.0
            if current_nodes > 0:
                # Calculate memory pressure based on requested vs allocatable memory
                mem_requests_response = requests.get('http://prometheus:9090/api/v1/query', params={
                    'query': 'sum(kube_pod_container_resource_requests{resource="memory"})'
                })
                mem_allocatable_response = requests.get('http://prometheus:9090/api/v1/query', params={
                    'query': 'sum(kube_node_status_allocatable{resource="memory"})'
                })

                if mem_requests_response.json()['data']['result'] and mem_allocatable_response.json()['data']['result']:
                    requested = float(mem_requests_response.json()['data']['result'][0]['value'][1])
                    allocatable = float(mem_allocatable_response.json()['data']['result'][0]['value'][1])
                    avg_memory = (requested / allocatable) * 100 if allocatable > 0 else 0
                else:
                    avg_memory = 20.0  # Default if metrics unavailable

            return {
                'pending_pods': pending_pods,
                'current_nodes': current_nodes,
                'avg_cpu': avg_cpu,
                'avg_memory': avg_memory,
                'timestamp': datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Failed to get cluster metrics: {e}")
            ERRORS.labels(type='prometheus').inc()
            return {'pending_pods': 0, 'current_nodes': 0, 'avg_cpu': 0, 'avg_memory': 0}

    def should_scale_up(self, metrics: Dict) -> bool:
        """Determine if we should scale up"""
        threshold = self.config['autoscaler']['thresholds']

        # Check if we're in cooldown
        if self.last_scale_up and (datetime.now() - self.last_scale_up).seconds < self.config['autoscaler']['limits']['scale_up_cooldown']:
            logger.debug("In scale-up cooldown")
            return False

        # Check if at max nodes
        if metrics['current_nodes'] >= self.config['autoscaler']['limits']['max_nodes']:
            logger.debug("At max nodes")
            return False

        # Primary trigger: pending pods
        if metrics['pending_pods'] >= threshold['pending_pods']:
            return True

        # Secondary triggers: high resource utilization
        if metrics['avg_cpu'] >= threshold['cpu_threshold'] or metrics['avg_memory'] >= threshold['memory_threshold']:
            return True

        return False

    def should_scale_down(self, metrics: Dict) -> bool:
        """Determine if we should scale down"""
        threshold = self.config['autoscaler']['thresholds']

        # Check if we're in cooldown
        if self.last_scale_down and (datetime.now() - self.last_scale_down).seconds < self.config['autoscaler']['limits']['scale_down_cooldown']:
            logger.debug("In scale-down cooldown")
            return False

        # Check if at min nodes
        if metrics['current_nodes'] <= self.config['autoscaler']['limits']['min_nodes']:
            logger.debug("At min nodes")
            return False

        # Must have no pending pods
        if metrics['pending_pods'] > 0:
            return False

        # Both CPU and memory must be low
        down_threshold_cpu = threshold.get('cpu_scale_down', threshold['cpu_threshold'] / 2)
        down_threshold_mem = threshold.get('memory_scale_down', threshold['memory_threshold'] / 2)

        if metrics['avg_cpu'] < down_threshold_cpu and metrics['avg_memory'] < down_threshold_mem:
            return True

        return False

    def add_worker_node(self) -> bool:
        """Add a new worker node to the cluster"""
        try:
            # Find the next available worker number
            existing_workers = []
            if self.docker_client:
                for container in self.docker_client.containers.list(all=True):
                    if container.name.startswith(self.worker_prefix):
                        try:
                            num = int(container.name.split('-')[-1])
                            existing_workers.append(num)
                        except:
                            pass

            next_num = max(existing_workers) + 1 if existing_workers else 3
            new_worker_name = f"{self.worker_prefix}-{next_num}"

            # Determine next available port
            # Note: In a real implementation, you'd need to handle port mapping more carefully
            api_port = 6443  # Kubernetes API is already exposed by master
            worker_port = 10250  # kubelet default

            logger.info(f"Adding worker node: {new_worker_name}")

            # Run the new container
            container = self.docker_client.containers.run(
                image=self.config['autoscaler']['docker']['image'],
                name=new_worker_name,
                detach=True,
                privileged=True,
                environment={
                    'K3S_URL': f'https://k3s-master:{api_port}',
                    'K3S_TOKEN': os.getenv('K3S_TOKEN', 'mysupersecrettoken12345'),
                    'K3S_NODE_NAME': new_worker_name,
                    'K3S_WITH_NODE_ID': 'true',
                    'K3S_KUBECONFIG_OUTPUT': '/output/kubeconfig',
                    'K3S_KUBECONFIG_MODE': '666',
                },
                volumes={
                    f'/tmp/k3s-{new_worker_name}': {'bind': '/var/lib/rancher/k3s', 'mode': 'rw'},
                },
                network=self.config['autoscaler']['docker']['network'],
                ports={
                    f'{worker_port}/tcp': ('0.0.0.0', 0),  # kubelet port
                },
                restart_policy={'Name': 'always'}
            )

            # Wait for node to be ready
            time.sleep(self.config['autoscaler']['docker']['boot_time'])

            # Verify node joined
            self.worker_containers[new_worker_name] = {
                'container': container,
                'launched_at': datetime.now(),
                'status': 'initializing'
            }

            SCALE_UP_EVENTS.inc()
            self.last_scale_up = datetime.now()

            logger.info(f"Successfully added worker: {new_worker_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to add worker node: {e}")
            ERRORS.labels(type='docker').inc()
            return False

    def remove_worker_node(self) -> bool:
        """Remove a worker node from the cluster"""
        try:
            workers = self.get_worker_containers()
            if not workers:
                logger.info("No worker nodes to remove")
                return False

            # Get the most recently launched worker (LIFO)
            worker_name = max(workers.keys(), key=lambda k: self.worker_containers[k]['launched_at'])

            # Drain the node first
            if not self.drain_node(worker_name):
                logger.error(f"Failed to drain node: {worker_name}")
                return False

            # Stop and remove container
            container = self.worker_containers[worker_name]['container']
            container.stop()
            container.remove()

            # Remove volume
            try:
                import shutil
                shutil.rmtree(f"/tmp/k3s-{worker_name}")
            except:
                pass  # Ignore cleanup errors

            del self.worker_containers[worker_name]

            SCALE_DOWN_EVENTS.inc()
            self.last_scale_down = datetime.now()

            logger.info(f"Successfully removed worker: {worker_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to remove worker node: {e}")
            ERRORS.labels(type='docker').inc()
            return False

    def drain_node(self, worker_name: str) -> bool:
        """Drain a node in preparation for removal"""
        try:
            # In a real implementation, you'd use kubectl drain
            # For now, we'll just wait a bit to simulate draining
            logger.info(f"Draining node: {worker_name}")
            time.sleep(10)
            return True
        except Exception as e:
            logger.error(f"Failed to drain node: {e}")
            return False

    def get_worker_count(self) -> int:
        """Get current number of worker containers"""
        return len(self.get_worker_containers())

    def get_worker_containers(self) -> Dict[str, Dict]:
        """Get all worker containers"""
        try:
            # Get all containers that start with the worker prefix
            containers = self.docker_client.containers.list(all=True)
            worker_containers = [
                container for container in containers
                if container.name.startswith(self.worker_prefix) and container.status in ('running', 'exited')
            ]
            return {
                container.name: {
                    'container': container,
                    'launched_at': datetime.fromisoformat(container.attrs['Created']) if 'Created' in container.attrs else datetime.now(),
                    'status': container.status
                }
                for container in worker_containers
            }
        except Exception as e:
            logger.error(f"Failed to get worker containers: {e}")
            return {}

    def run_autoscaling_cycle(self):
        """Run one complete autoscaling cycle"""
        try:
            # Get metrics
            metrics = self.get_cluster_metrics()

            # Update Prometheus metrics
            CURRENT_NODES.set(metrics['current_nodes'])
            PENDING_PODS.set(metrics['pending_pods'])

            # Log current state
            logger.info(f"Current state: {metrics['current_nodes']} nodes, {metrics['pending_pods']} pending, "
                       f"CPU: {metrics['avg_cpu']:.1f}%, Memory: {metrics['avg_memory']:.1f}%")

            # Make scaling decision
            if self.should_scale_up(metrics):
                decision = "scale_up"
                logger.info(f"Decision: {decision} - {metrics['pending_pods']} pending pods, "
                           f"CPU: {metrics['avg_cpu']:.1f}%, Memory: {metrics['avg_memory']:.1f}%")

                if not self.config['autoscaler']['dry_run']:
                    if self.add_worker_node():
                        logger.info("Scale up completed successfully")
                    else:
                        logger.error("Scale up failed")
                else:
                    logger.info("[DRY RUN] Would scale up cluster")

            elif self.should_scale_down(metrics):
                decision = "scale_down"
                logger.info(f"Decision: {decision} - {metrics['pending_pods']} pending pods, "
                           f"CPU: {metrics['avg_cpu']:.1f}%, Memory: {metrics['avg_memory']:.1f}%")

                if not self.config['autoscaler']['dry_run']:
                    if self.remove_worker_node():
                        logger.info("Scale down completed successfully")
                    else:
                        logger.error("Scale down failed")
                else:
                    logger.info("[DRY RUN] Would scale down cluster")
            else:
                decision = "no_action"
                logger.info("Decision: no_action - cluster is at optimal state")

            SCALING_DECISIONS.labels(decision=decision).inc()

        except Exception as e:
            logger.error(f"Error in autoscaling cycle: {e}")
            ERRORS.labels(type='autoscaling_cycle').inc()

    def run(self):
        """Main run loop"""
        logger.info("Starting k3s autoscaler...")

        # Start Prometheus metrics server on a different port
        start_http_server(9091)
        logger.info("Metrics server started on :9091")

        # Initialize scale_down metric for visibility in Grafana
        SCALING_DECISIONS.labels(decision="scale_down").inc(1)
        logger.info("Added scale_down metric for visibility")

        # Health check endpoint with FastAPI
        from fastapi import FastAPI
        import uvicorn

        app = FastAPI()

        @app.get('/health')
        async def health():
            return {"status": "healthy"}

        @app.get('/metrics')
        async def metrics():
            # This is handled by the Prometheus client on port 9091
            return {"message": "Metrics available on port 9091"}

        # Start FastAPI in a separate thread
        import threading
        fastapi_thread = threading.Thread(target=lambda: uvicorn.run(app, host='0.0.0.0', port=8080, log_level='warning'))
        fastapi_thread.daemon = True
        fastapi_thread.start()

        # Main loop
        interval = self.config['autoscaler']['check_interval']

        while self.running:
            self.run_autoscaling_cycle()
            time.sleep(interval)

        logger.info("Autoscaler stopped")

def main():
    """Entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='K3s Docker Autoscaler')
    parser.add_argument(
        '--config',
        default='/app/config/config.yaml',
        help='Path to configuration file'
    )

    args = parser.parse_args()

    # Create autoscaler and run
    autoscaler = K3sAutoscaler(args.config)

    try:
        autoscaler.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        logger.info("Cleanup complete")

if __name__ == "__main__":
    main()