#!/usr/bin/env python3
"""
Core autoscaler logic module
"""

import logging
import time
import docker
import os
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from database import DatabaseManager, WorkerNode, NodeStatus, ScalingEventType
from .metrics import MetricsCollector
from .scaling import ScalingEngine
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

        # Initialize default state
        self.worker_prefix = "k3s-worker"
        self.last_scale_up = None
        self.last_scale_down = None

        logger.info("K3s Autoscaler initialized with database support")

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

            # Update database with current state
            log_section(logger, "DATABASE UPDATE")
            self._update_state(metrics_data, decision_result)

            return {
                "metrics": metrics_data,
                "decision": decision_result,
                "timestamp": datetime.utcnow().isoformat()
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
                "timestamp": datetime.utcnow().isoformat()
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
        """Scale up by adding worker nodes"""
        logger.info(f"Scaling up by {count} nodes")

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

        # Add workers
        added = 0
        for i in range(count):
            worker_node = self._create_worker_node()
            if worker_node:
                self.database.add_worker(worker_node)
                added += 1

        if added > 0:
            # Set cooldown
            self.database.set_cooldown("scale_up", self.config['autoscaler']['limits']['scale_up_cooldown'])
            self.database.set_cluster_state("last_scale_up", datetime.utcnow().isoformat())
            self.last_scale_up = datetime.utcnow()

            logger.info(f"Successfully scaled up by {added} nodes")
            return True
        else:
            logger.warning("No nodes were added during scale up")
            return False

    def _scale_down(self, count: int, decision: Dict[str, Any], metrics: Dict[str, Any] = None) -> bool:
        """Scale down by removing worker nodes"""
        logger.info(f"Scaling down by {count} nodes")

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

        # Check minimum nodes using actual cluster metrics
        if metrics:
            current_count = metrics.current_nodes if hasattr(metrics, 'current_nodes') else 0
        else:
            # Fallback to database count (may be out of sync)
            current_count = self.database.get_worker_count()

        min_nodes = self.config['autoscaler']['limits']['min_nodes']
        logger.info(f"Worker count check: current={current_count}, min={min_nodes}")
        if current_count <= min_nodes:
            logger.info(f"Cannot scale down: at minimum nodes ({min_nodes})")
            return False

        # Get workers (most recently launched first)
        workers = self.database.get_all_workers()
        workers_to_remove = workers[-count:] if count < len(workers) else workers

        removed = 0
        for worker in workers_to_remove:
            if self.database.remove_worker(worker.node_name):
                removed += 1

        if removed > 0:
            # Set cooldown
            self.database.set_cooldown("scale_down", self.config['autoscaler']['limits']['scale_down_cooldown'])
            self.database.set_cluster_state("last_scale_down", datetime.utcnow().isoformat())
            self.last_scale_down = datetime.utcnow()

            logger.info(f"Successfully scaled down by {removed} nodes")
            return True
        else:
            logger.warning("No nodes were removed during scale down")
            return False

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
                launched_at=datetime.utcnow(),
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
                "age_seconds": int((datetime.utcnow() - worker.launched_at).total_seconds())
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
                        launched_at=datetime.utcnow(),
                        metadata={
                            "created_by": "docker_compose",
                            "container_image": container.image.tags[0] if container.image.tags else "unknown",
                            "network": self.config['autoscaler']['docker']['network'],
                            "synced_at": datetime.utcnow().isoformat()
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