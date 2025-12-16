#!/usr/bin/env python3
"""
Core autoscaler logic module
"""

import logging
import time
import docker
import os
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from database import DatabaseManager, WorkerNode, NodeStatus, ScalingEventType
from .metrics import MetricsCollector
from .scaling import ScalingEngine
from .async_scaling import AsyncScalingManager
from core.logging_config import log_separator, log_section

logger = logging.getLogger(__name__)


class K3sAutoscaler:
    """Core autoscaler that makes scaling decisions based on metrics"""

    def __init__(self, config: Dict, database: DatabaseManager):
        """
        Initialize the autoscaler

        Args:
            config: Autoscaler configuration
            database: Database manager instance
        """
        self.config = config
        self.database = database
        self.running = True

        # Initialize components
        self.metrics = MetricsCollector(config, database)
        self.scaling = ScalingEngine(config, database)

        # Initialize async scaling manager
        self.async_scaling = AsyncScalingManager(
            config=config,
            max_workers=config.get('autoscaler', {}).get('max_concurrent_operations', 3)
        )

        # Initialize default state
        self.worker_prefix = "k3s-worker"
        self.last_scale_up = None
        self.last_scale_down = None

        logger.info("K3s Autoscaler initialized with database support and async operations")

        # Sync database with actual cluster state on startup
        self._sync_database_with_cluster()

    def run_cycle(self) -> Dict[str, Any]:
        """
        Run a complete autoscaling cycle

        Returns:
            Dict containing cycle results
        """
        # Add cycle separator
        cycle_number = getattr(self, '_cycle_counter', 0) + 1
        self._cycle_counter = cycle_number
        log_separator(logger, f"AUTOSCALING CYCLE #{cycle_number}", 60)

        try:
            # Collect metrics
            log_section(logger, "METRICS COLLECTION")
            metrics_data = self.metrics.collect()
            logger.info(f"Collected metrics: {metrics_data.current_nodes} nodes, "
                       f"{metrics_data.pending_pods} pending, "
                       f"CPU: {metrics_data.avg_cpu:.1f}%, Memory: {metrics_data.avg_memory:.1f}%")

            # Make scaling decision
            log_section(logger, "SCALING DECISION")
            decision_result = self.scaling.evaluate_scaling(metrics_data)

            # Execute scaling decision
            dry_run_enabled = self.config['autoscaler']['dry_run']
            logger.info(f"Dry-run mode: {dry_run_enabled}, Should scale: {decision_result.get('should_scale', False)}")

            if decision_result['should_scale'] and not dry_run_enabled:
                log_section(logger, "SCALING EXECUTION")
                success = self._execute_scaling(decision_result, metrics_data)
                decision_result['success'] = success
            elif dry_run_enabled:
                log_section(logger, "DRY RUN MODE")
                logger.info("Dry-run mode: Skipping actual scaling execution")
                decision_result['dry_run'] = True
                decision_result['success'] = True

            # Update Prometheus metrics if configured
            self._update_state(metrics_data, decision_result)

            return {
                "metrics": metrics_data,
                "decision": decision_result,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Error in autoscaling cycle: {e}")
            # Record error in database
            self.database.record_scaling_event(
                ScalingEventType.ERROR,
                old_count=self.database.get_worker_count(),
                new_count=self.database.get_worker_count(),
                reason=f"Cycle error: {str(e)}",
                metrics={},
                details={"error": str(e)}
            )
            return {
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    def _execute_scaling(self, decision: Dict[str, Any], metrics: Dict[str, Any] = None) -> bool:
        """Execute a scaling decision"""
        action = decision['action']
        count = decision.get('count', 1)

        try:
            if action == "scale_up":
                return self._scale_up(count, decision)
            elif action == "scale_down":
                return self._scale_down(count, decision, metrics)
            else:
                logger.warning(f"Unknown scaling action: {action}")
                return False
        except Exception as e:
            logger.error(f"Failed to execute scaling {action}: {e}")
            return False

    def _scale_up(self, count: int, decision: Dict[str, Any]) -> bool:
        """Scale up by adding worker nodes using async operations"""
        logger.info(f"Scaling up by {count} nodes (async mode)")

        # Check both cooldowns to prevent rapid oscillation
        if self.database.is_cooldown_active("scale_up"):
            remaining = self.database.get_cooldown_remaining("scale_up")
            logger.info(f"Scale up cooldown active: {remaining}s remaining")
            return False

        # Also check if scale_down is recently active (optional: prevent immediate reverse scaling)
        # if self.database.is_cooldown_active("scale_down"):
        #     remaining = self.database.get_cooldown_remaining("scale_down")
        #     logger.info(f"Recent scale down, waiting: {remaining}s remaining")
        #     return False

        try:
            # Initialize Docker client if not already done
            if not hasattr(self, 'docker_client'):
                if self.config['autoscaler']['dry_run']:
                    logger.info("Dry-run mode: skipping Docker container creation")
                    return False
                self.docker_client = docker.from_env()
                self.docker_client.ping()
                logger.info("Docker client initialized")

            # Initialize Kubernetes API for async scaling manager
            if hasattr(self.metrics, 'k8s_api') and self.metrics.k8s_api:
                self.async_scaling.set_kubernetes_api(self.metrics.k8s_api)
                logger.info("Kubernetes API set for async operations")

            # Run async scaling operation using existing event loop
            try:
                # Try to get current event loop
                loop = asyncio.get_running_loop()
                # If we're in an async context (FastAPI), we need to run in a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        lambda: asyncio.run(self.async_scaling.scale_up_concurrent(count, self.docker_client, self.database))
                    )
                    successful_workers, error_messages = future.result()
            except RuntimeError:
                # No running loop, we can create a new one
                successful_workers, error_messages = asyncio.run(
                    self.async_scaling.scale_up_concurrent(count, self.docker_client, self.database)
                )

            # Log results
            if error_messages:
                for error in error_messages:
                    logger.error(f"Scale-up error: {error}")

            if not successful_workers:
                logger.warning("No workers were added during scale-up")
                return False

            # Set cooldown after successful scale-up
            self.database.set_cooldown("scale_up", self.config['autoscaler']['limits']['scale_up_cooldown'])
            self.database.set_cluster_state("last_scale_up", datetime.now(timezone.utc).isoformat())
            self.last_scale_up = datetime.now(timezone.utc)

            logger.info(f"Successfully scaled up by {len(successful_workers)} nodes in async mode")
            return True

        except Exception as e:
            logger.error(f"Async scale-up failed: {e}")
            return False

    def _scale_down(self, count: int, decision: Dict[str, Any], metrics: Dict[str, Any] = None) -> bool:
        """Scale down by removing worker nodes using async operations"""
        logger.info(f"Scaling down by {count} nodes (async mode)")

        # Check both cooldowns to prevent rapid oscillation
        if self.database.is_cooldown_active("scale_down"):
            remaining = self.database.get_cooldown_remaining("scale_down")
            logger.info(f"Scale down cooldown active: {remaining}s remaining")
            return False

        # CRITICAL: Also check if scale_up is active to prevent immediate reverse scaling
        if self.database.is_cooldown_active("scale_up"):
            remaining = self.database.get_cooldown_remaining("scale_up")
            logger.info(f"Recent scale up, waiting: {remaining}s remaining before scale down")
            return False

        # Get actual Kubernetes node count to make accurate minimum node decisions
        actual_k8s_nodes = 0
        actual_docker_nodes = 0
        db_workers = self.database.get_all_workers()
        min_nodes = self.config['autoscaler']['limits']['min_nodes']

        # Get Docker container count for workers
        if not hasattr(self, 'docker_client'):
            self.docker_client = docker.from_env()

        try:
            containers = self.docker_client.containers.list(all=True)
            actual_docker_nodes = len([c for c in containers
                                     if c.name and c.name.startswith(self.worker_prefix)])
            logger.info(f"Actual Docker worker containers: {actual_docker_nodes}")
        except Exception as e:
            logger.warning(f"Could not get Docker container count: {e}")

        # Count actual Kubernetes nodes
        if hasattr(self.metrics, 'k8s_api') and self.metrics.k8s_api:
            try:
                k8s_nodes = self.metrics.k8s_api.list_node()
                actual_k8s_nodes = len([n for n in k8s_nodes.items
                                      if not n.metadata.labels.get('node-role.kubernetes.io/control-plane')])
                logger.info(f"Actual Kubernetes nodes: {actual_k8s_nodes}")

                # Cross-validate counts
                if abs(actual_k8s_nodes - actual_docker_nodes) > 1:
                    logger.warning(f"Node count mismatch: K8s={actual_k8s_nodes}, Docker={actual_docker_nodes}")
                    # Use the lower count for safety
                    actual_nodes = min(actual_k8s_nodes, actual_docker_nodes)
                else:
                    actual_nodes = actual_k8s_nodes

            except Exception as e:
                logger.warning(f"Could not get Kubernetes node count: {e}")
                # Fallback to Docker count
                actual_nodes = actual_docker_nodes
        else:
            # Fallback to Docker count
            actual_nodes = actual_docker_nodes
            logger.warning("Kubernetes API not available, using Docker count for node detection")

        # Check minimum nodes against validated node count
        logger.info(f"Node count check: k8s_nodes={actual_k8s_nodes}, docker={actual_docker_nodes}, actual={actual_nodes}, db_workers={len(db_workers)}, min={min_nodes}")

        # Simple minimum check: allow scaling down to exactly min_nodes
        if actual_nodes <= min_nodes:
            logger.info(f"Cannot scale down: at minimum nodes (current={actual_nodes}, min={min_nodes})")
            return False

        # Emergency stop: only trigger if we're about to violate minimum
        # This is just an extra safety check
        if actual_nodes - count < min_nodes:
            logger.error(f"EMERGENCY STOP: Cannot remove {count} nodes, would go below minimum (current={actual_nodes}, min={min_nodes})")
            logger.error("Manual intervention required - cluster at minimum capacity")
            return False

        # Get workers (most recently launched first)
        workers = self.database.get_all_workers()

        # If database is empty but we have Docker containers, create temporary worker objects
        if not workers and actual_docker_nodes > 0:
            logger.warning(f"Database has no workers but {actual_docker_nodes} Docker containers exist. Creating temporary worker objects.")
            containers = [c for c in self.docker_client.containers.list(all=True)
                          if c.name and c.name.startswith(self.worker_prefix)]

            # Sort by creation time (newest first)
            containers.sort(key=lambda c: c.attrs['Created'], reverse=True)

            # Create temporary WorkerNode objects for removal
            from database import WorkerNode, NodeStatus
            workers = []
            for container in containers[:count]:
                worker = WorkerNode(
                    node_name=container.name,
                    container_id=container.id,
                    container_name=container.name,
                    status=NodeStatus.READY,  # Assume ready for removal
                    launched_at=datetime.now(timezone.utc),
                    metadata={"synced_from_docker": True}
                )
                workers.append(worker)

        workers_to_remove = workers[-count:] if count < len(workers) else workers

        try:
            # Initialize Docker client if not already done
            if not hasattr(self, 'docker_client'):
                self.docker_client = docker.from_env()
                self.docker_client.ping()

            # Initialize Kubernetes API for async scaling manager
            if hasattr(self.metrics, 'k8s_api') and self.metrics.k8s_api:
                self.async_scaling.set_kubernetes_api(self.metrics.k8s_api)
                logger.info("Kubernetes API set for async operations")

            # Run async scale-down operation using existing event loop
            try:
                # Try to get current event loop
                loop = asyncio.get_running_loop()
                # If we're in an async context (FastAPI), we need to run in a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        lambda: asyncio.run(self.async_scaling.scale_down_concurrent(workers_to_remove, self.docker_client, self.database))
                    )
                    successful_removals, error_messages = future.result()
            except RuntimeError:
                # No running loop, we can create a new one
                successful_removals, error_messages = asyncio.run(
                    self.async_scaling.scale_down_concurrent(workers_to_remove, self.docker_client, self.database)
                )

            # Log results
            if error_messages:
                for error in error_messages:
                    logger.error(f"Scale-down error: {error}")

            if successful_removals:
                # Set cooldown after successful scale-down
                self.database.set_cooldown("scale_down", self.config['autoscaler']['limits']['scale_down_cooldown'])
                self.database.set_cluster_state("last_scale_down", datetime.now(timezone.utc).isoformat())
                self.last_scale_down = datetime.now(timezone.utc)

                logger.info(f"Successfully scaled down by {len(successful_removals)} nodes in async mode")
                return True
            else:
                logger.warning("No nodes were removed during scale down")
                return False

        except Exception as e:
            logger.error(f"Async scale-down failed: {e}")
            return False

    def _create_worker_node_atomic(self) -> Optional[WorkerNode]:
        """Create a new worker node (Docker container only, no database updates)"""
        try:
            # Find the next available worker number
            existing_workers = []
            containers = self.docker_client.containers.list(all=True)
            for container in containers:
                if container.name and container.name.startswith(self.worker_prefix):
                    try:
                        num = int(container.name.split('-')[-1])
                        existing_workers.append(num)
                    except:
                        pass

            next_num = max(existing_workers) + 1 if existing_workers else 3
            node_name = f"{self.worker_prefix}-{next_num}"

            logger.info(f"Creating worker node: {node_name}")

            # Create the container
            container = self.docker_client.containers.run(
                image=self.config['autoscaler']['docker']['image'],
                name=node_name,
                detach=True,
                privileged=True,
                environment={
                    'K3S_URL': f"https://{self.config['autoscaler']['kubernetes'].get('server_host', 'k3s-master')}:6443",
                    'K3S_TOKEN': os.getenv('K3S_TOKEN', 'mysupersecrettoken12345'),
                    'K3S_NODE_NAME': node_name,
                    'K3S_WITH_NODE_ID': 'true'
                },
                volumes={
                    f'/tmp/k3s-{node_name}': {'bind': '/var/lib/rancher/k3s', 'mode': 'rw'},
                },
                network=self.config['autoscaler']['docker']['network'],
                restart_policy={'Name': 'always'}
            )

            # Create worker node record (without database updates)
            worker_node = WorkerNode(
                node_name=node_name,
                container_id=container.id,
                container_name=node_name,
                status=NodeStatus.INITIALIZING,
                launched_at=datetime.now(timezone.utc),
                metadata={
                    "created_by": "autoscaler",
                    "container_image": self.config['autoscaler']['docker']['image'],
                    "network": self.config['autoscaler']['docker']['network']
                }
            )

            logger.info(f"Created container: {node_name}")
            return worker_node

        except Exception as e:
            logger.error(f"Failed to create worker node: {e}")
            return None

    def _wait_for_kubernetes_node(self, node_name: str, timeout: int = 60) -> bool:
        """Wait for a node to register in Kubernetes"""
        logger.info(f"Waiting for node {node_name} to register in Kubernetes...")

        if not hasattr(self.metrics, 'k8s_api') or not self.metrics.k8s_api:
            logger.warning("Kubernetes API not available, skipping node verification")
            return True  # Assume success if we can't verify

        start_time = datetime.now(timezone.utc)
        while (datetime.now(timezone.utc) - start_time).total_seconds() < timeout:
            try:
                # Check if node exists in Kubernetes
                node = self.metrics.k8s_api.read_node(name=node_name)

                # Check if node is ready
                if node.status.conditions:
                    for condition in node.status.conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            logger.info(f"Node {node_name} is ready in Kubernetes")
                            return True

                logger.debug(f"Node {node_name} found but not ready yet")

            except Exception as e:
                logger.debug(f"Node {node_name} not yet registered: {e}")

            time.sleep(5)

        logger.warning(f"Timeout waiting for node {node_name} to register in Kubernetes")
        return False

    def _drain_kubernetes_node(self, node_name: str, timeout: int = 120) -> bool:
        """Drain a Kubernetes node before removal"""
        if not hasattr(self.metrics, 'k8s_api') or not self.metrics.k8s_api:
            logger.warning("Kubernetes API not available, skipping node drain")
            return True

        try:
            from kubernetes import client as k8s_client

            # Drain the node
            logger.info(f"Draining Kubernetes node: {node_name}")

            # 1. Mark the node as unschedulable
            try:
                node = self.metrics.k8s_api.read_node(name=node_name)

                # Create a patch to mark as unschedulable
                patch_body = {
                    "spec": {
                        "unschedulable": True
                    }
                }
                self.metrics.k8s_api.patch_node(name=node_name, body=patch_body)
                logger.info(f"Marked node {node_name} as unschedulable")
            except Exception as e:
                logger.warning(f"Could not mark node {node_name} as unschedulable: {e}")
                # Continue with eviction anyway

            # 2. Evict all pods from the node (except system pods)
            pods = self.metrics.k8s_api.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}"
            )

            evicted_count = 0
            for pod in pods.items:
                # Skip system pods and daemonsets
                if pod.metadata.namespace in ["kube-system", "kube-public"]:
                    continue
                if pod.metadata.owner_references:
                    for owner in pod.metadata.owner_references:
                        if owner.kind == "DaemonSet":
                            continue

                try:
                    # Use eviction API to gracefully evict pods
                    eviction = k8s_client.V1Eviction(
                        metadata=k8s_client.V1ObjectMeta(
                            name=pod.metadata.name,
                            namespace=pod.metadata.namespace
                        )
                    )

                    # Create eviction using the core API
                    self.metrics.k8s_api.create_namespaced_pod_eviction(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace,
                        body=eviction
                    )
                    logger.info(f"Evicted pod {pod.metadata.name}/{pod.metadata.namespace}")
                    evicted_count += 1
                except Exception as e:
                    # If eviction fails, log but continue
                    logger.debug(f"Could not evict pod {pod.metadata.name}: {e}")

            # 3. Force delete the node from Kubernetes
            try:
                logger.info(f"Deleting node {node_name} from Kubernetes...")
                self.metrics.k8s_api.delete_node(
                    name=node_name,
                    body=k8s_client.V1DeleteOptions(
                        grace_period_seconds=0,
                        propagation_policy='Background'
                    )
                )
                logger.info(f"Initiated node deletion: {node_name}")
            except Exception as e:
                logger.error(f"Failed to delete node {node_name}: {e}")
                # Continue anyway - the container will be removed

            logger.info(f"Successfully drained Kubernetes node: {node_name} (evicted {evicted_count} pods)")
            return True

        except Exception as e:
            logger.error(f"Error draining node {node_name}: {e}")
            return False

    def _wait_for_kubernetes_node_removal(self, node_name: str, timeout: int = 60) -> bool:
        """Wait for a node to be removed from Kubernetes"""
        logger.info(f"Waiting for node {node_name} to be removed from Kubernetes...")

        if not hasattr(self.metrics, 'k8s_api') or not self.metrics.k8s_api:
            logger.warning("Kubernetes API not available, skipping node removal verification")
            return True

        start_time = datetime.now(timezone.utc)
        while (datetime.now(timezone.utc) - start_time).total_seconds() < timeout:
            try:
                # Check if node still exists in Kubernetes
                self.metrics.k8s_api.read_node(name=node_name)
                # Node still exists, wait longer
                logger.debug(f"Node {node_name} still exists in Kubernetes")
            except Exception as e:
                # Node not found - good!
                logger.info(f"Node {node_name} removed from Kubernetes")
                return True

            time.sleep(5)

        logger.warning(f"Timeout waiting for node {node_name} removal from Kubernetes")
        return False

    def _rollback_created_nodes(self, created_nodes: List[WorkerNode]):
        """Rollback created nodes if scale up fails"""
        logger.warning(f"Rolling back {len(created_nodes)} created nodes")

        for worker_node in created_nodes:
            try:
                # Remove Docker container
                container = self.docker_client.containers.get(worker_node.container_id)
                container.remove(force=True)
                logger.info(f"Rolled back container: {worker_node.node_name}")
            except docker.errors.NotFound:
                logger.warning(f"Container not found during rollback: {worker_node.node_name}")
            except Exception as e:
                logger.error(f"Failed to rollback container {worker_node.node_name}: {e}")

    def _create_worker_node(self) -> Optional[WorkerNode]:
        """Create a new worker node by launching a Docker container"""
        try:
            # Initialize Docker client if not already done
            if not hasattr(self, 'docker_client'):
                if self.config['autoscaler']['dry_run']:
                    logger.info("Dry-run mode: skipping Docker container creation")
                    return None
                self.docker_client = docker.from_env()
                self.docker_client.ping()
                logger.info("Docker client initialized")

            # Find the next available worker number
            existing_workers = []
            containers = self.docker_client.containers.list(all=True)
            for container in containers:
                if container.name and container.name.startswith(self.worker_prefix):
                    try:
                        num = int(container.name.split('-')[-1])
                        existing_workers.append(num)
                    except:
                        pass

            next_num = max(existing_workers) + 1 if existing_workers else 3
            node_name = f"{self.worker_prefix}-{next_num}"

            logger.info(f"Creating worker node: {node_name}")

            # Create the container
            container = self.docker_client.containers.run(
                image=self.config['autoscaler']['docker']['image'],
                name=node_name,
                detach=True,
                privileged=True,
                environment={
                    'K3S_URL': f"https://{self.config['autoscaler']['kubernetes'].get('server_host', 'k3s-master')}:6443",
                    'K3S_TOKEN': os.getenv('K3S_TOKEN', 'mysupersecrettoken12345'),
                    'K3S_NODE_NAME': node_name,
                    'K3S_WITH_NODE_ID': 'true'
                },
                volumes={
                    f'/tmp/k3s-{node_name}': {'bind': '/var/lib/rancher/k3s', 'mode': 'rw'},
                },
                network=self.config['autoscaler']['docker']['network'],
                restart_policy={'Name': 'always'}
            )

            # Create worker node record
            worker_node = WorkerNode(
                node_name=node_name,
                container_id=container.id,
                container_name=node_name,
                status=NodeStatus.INITIALIZING,
                launched_at=datetime.now(timezone.utc),
                metadata={
                    "created_by": "autoscaler",
                    "container_image": self.config['autoscaler']['docker']['image'],
                    "network": self.config['autoscaler']['docker']['network']
                }
            )

            # Wait for node to initialize
            boot_time = self.config['autoscaler']['docker'].get('boot_time', 30)
            logger.info(f"Waiting {boot_time}s for node {node_name} to initialize...")
            time.sleep(boot_time)

            # Update status to ready after initialization time
            worker_node.status = NodeStatus.READY
            self.database.update_worker_status(node_name, NodeStatus.READY)

            logger.info(f"Successfully created worker node: {node_name}")
            return worker_node

        except Exception as e:
            logger.error(f"Failed to create worker node: {e}")
            return None

    def _update_state(self, metrics: Any, decision: Dict[str, Any]):
        """Update cluster state in database"""
        # Update Prometheus metrics if configured
        if hasattr(self, 'prometheus_metrics'):
            # Convert Pydantic model to dict if needed
            if hasattr(metrics, 'dict'):
                self.prometheus_metrics.update(metrics.dict())
            else:
                self.prometheus_metrics.update(metrics)

    def get_status(self) -> Dict[str, Any]:
        """Get current autoscaler status"""
        try:
            db_health = self.database.check_database_health()

            return {
                "running": self.running,
                "worker_count": self.database.get_worker_count(),
                "last_scale_up": self.database.get_cluster_state("last_scale_up"),
                "last_scale_down": self.database.get_cluster_state("last_scale_down"),
                "database_health": db_health,
                "cooldowns": {
                    "scale_up": {
                        "active": self.database.is_cooldown_active("scale_up"),
                        "remaining": self.database.get_cooldown_remaining("scale_up")
                    },
                    "scale_down": {
                        "active": self.database.is_cooldown_active("scale_down"),
                        "remaining": self.database.get_cooldown_remaining("scale_down")
                    }
                }
            }
        except Exception as e:
            return {
                "error": str(e),
                "running": self.running
            }

    def stop(self):
        """Stop the autoscaler"""
        logger.info("Stopping autoscaler...")
        self.running = False

    def cleanup(self):
        """Cleanup resources"""
        try:
            # Close database connections
            self.database.close()
            logger.info("Autoscaler cleanup completed")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def cleanup_orphaned_nodes(self) -> Dict[str, Any]:
        """Clean up orphaned Kubernetes nodes that don't have corresponding containers"""
        cleanup_result = {
            "orphaned_nodes": [],
            "cleaned_nodes": [],
            "errors": []
        }

        if not hasattr(self.metrics, 'k8s_api') or not self.metrics.k8s_api:
            logger.warning("Kubernetes API not available, skipping orphaned node cleanup")
            return cleanup_result

        try:
            # Get all Kubernetes nodes (excluding control plane)
            k8s_nodes = self.metrics.k8s_api.list_node()
            worker_nodes = [n for n in k8s_nodes.items
                          if not n.metadata.labels.get('node-role.kubernetes.io/control-plane')]

            # Get all Docker containers
            if not hasattr(self, 'docker_client'):
                self.docker_client = docker.from_env()

            containers = self.docker_client.containers.list(all=True)
            container_names = {c.name for c in containers if c.name and c.name.startswith(self.worker_prefix)}

            # Find orphaned nodes (K8s nodes without Docker containers)
            for node in worker_nodes:
                node_name = node.metadata.name
                if node_name not in container_names:
                    cleanup_result["orphaned_nodes"].append(node_name)
                    logger.warning(f"Found orphaned Kubernetes node: {node_name}")

                    # Force delete the orphaned node
                    try:
                        logger.info(f"Force deleting orphaned node: {node_name}")
                        self.metrics.k8s_api.delete_node(
                            name=node_name,
                            body={
                                "gracePeriodSeconds": 0,
                                "propagationPolicy": "Background"
                            }
                        )
                        cleanup_result["cleaned_nodes"].append(node_name)
                        logger.info(f"Successfully deleted orphaned node: {node_name}")
                    except Exception as e:
                        error_msg = f"Failed to delete orphaned node {node_name}: {e}"
                        cleanup_result["errors"].append(error_msg)
                        logger.error(error_msg)

            if cleanup_result["cleaned_nodes"]:
                logger.info(f"Cleaned up {len(cleanup_result['cleaned_nodes'])} orphaned Kubernetes nodes")

        except Exception as e:
            error_msg = f"Error during orphaned node cleanup: {e}"
            cleanup_result["errors"].append(error_msg)
            logger.error(error_msg)

        return cleanup_result

    def get_scaling_history(self, limit: int = 50) -> List[Dict]:
        """Get recent scaling history"""
        return self.database.get_recent_events(limit)

    def get_worker_details(self) -> List[Dict]:
        """Get detailed information about all workers"""
        workers = self.database.get_all_workers()
        details = []

        for worker in workers:
            # Get health status from Redis
            health = self.database.get_node_health(worker.node_name)

            details.append({
                "name": worker.node_name,
                "status": worker.status.value,
                "container_id": worker.container_id,
                "launched_at": worker.launched_at.isoformat(),
                "last_seen": worker.last_seen.isoformat() if worker.last_seen else None,
                "metadata": worker.metadata or {},
                "health": health,
                "age_seconds": int((datetime.now(timezone.utc) - worker.launched_at).total_seconds())
            })

        return details

    def _sync_database_with_cluster(self):
        """Sync database records with actual cluster state"""
        try:
            # Get current cluster state
            metrics_data = self.metrics.collect()
            actual_nodes = metrics_data.current_nodes

            # Get workers from database
            db_workers = self.database.get_all_workers()
            db_node_names = {w.node_name for w in db_workers}

            # Get actual worker containers from Docker
            if not hasattr(self, 'docker_client'):
                self.docker_client = docker.from_env()
                self.docker_client.ping()

            containers = self.docker_client.containers.list(all=True)
            actual_containers = {}
            for container in containers:
                if container.name and container.name.startswith(self.worker_prefix):
                    actual_containers[container.name] = container

            logger.info(f"Syncing database with cluster: {len(actual_containers)} containers, {len(db_workers)} DB records")

            # Add missing workers to database
            for container_name, container in actual_containers.items():
                if container_name not in db_node_names:
                    worker_node = WorkerNode(
                        node_name=container_name,
                        container_id=container.id,
                        container_name=container_name,
                        status=NodeStatus.READY if container.status == 'running' else NodeStatus.STOPPED,
                        launched_at=datetime.now(timezone.utc),
                        metadata={
                            "created_by": "docker_compose",
                            "container_image": container.image.tags[0] if container.image.tags else "unknown",
                            "network": self.config['autoscaler']['docker']['network'],
                            "synced_at": datetime.now(timezone.utc).isoformat()
                        }
                    )
                    self.database.add_worker(worker_node)
                    logger.info(f"Added existing worker to database: {container_name}")

            # Mark removed workers as REMOVED in database
            for worker in db_workers:
                if worker.node_name not in actual_containers:
                    self.database.update_worker_status(worker.node_name, NodeStatus.REMOVED)
                    logger.info(f"Marked worker as REMOVED in database: {worker.node_name}")

            logger.info("Database sync with cluster completed")

        except Exception as e:
            logger.error(f"Failed to sync database with cluster: {e}")
            # Don't fail initialization, just log the error