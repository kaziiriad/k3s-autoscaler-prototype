#!/usr/bin/env python3
"""
Core autoscaler logic module
"""
import json  # Import at function level to avoid circular imports
import logging
import time
import docker
import os
import asyncio
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from database import DatabaseManager, WorkerNode, NodeStatus, ScalingEventType
from config.settings import settings, REDIS_KEYS
from .metrics import MetricsCollector
from .scaling import ScalingEngine
from .async_scaling import AsyncScalingManager
from core.logging_config import log_separator, log_section
import concurrent.futures

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
        self.worker_prefix = settings.autoscaler.worker_prefix

        # Initialize last scaling timestamps to prevent datetime comparison errors
        self.last_scale_up = None
        self.last_scale_down = None

        logger.info("K3s Autoscaler initialized with database support and async operations")

        # Perform startup reconciliation to ensure data integrity
        logger.info("Performing startup reconciliation...")
        from core.startup_reconciliation import StartupReconciler

        startup_reconciler = StartupReconciler(self)
        reconciliation_results = startup_reconciler.reconcile_on_startup()

        if reconciliation_results.get("errors"):
            logger.error(f"Startup reconciliation errors: {reconciliation_results['errors']}")

        logger.info(f"Startup reconciliation complete: {len(reconciliation_results['actions'])} actions")

        # Sync database with actual cluster state on startup
        self._sync_database_with_cluster()

        # Initialize permanent workers in Redis
        self._initialize_permanent_workers()

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

            # Check for optimal state and log it
            if (not decision_result.get('should_scale', False) and
                metrics_data.current_nodes == self.config['autoscaler']['limits']['min_nodes'] and
                metrics_data.avg_cpu < 50 and metrics_data.avg_memory < 80 and
                metrics_data.pending_pods == 0):

                logger.info(f"✓ Cluster at optimal state: {metrics_data.current_nodes} workers, "
                          f"CPU: {metrics_data.avg_cpu:.1f}%, Memory: {metrics_data.avg_memory:.1f}%")

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

        # Check if scaling operation already in progress
        if hasattr(self.database, 'redis') and self.database.redis:
            lock_key = REDIS_KEYS['SCALING_LOCK']

            # Check if lock exists and when it was created
            if self.database.redis.exists(lock_key):
                # Get the lock value to check timestamp
                lock_value = self.database.redis.get(lock_key, deserialize=False)
                if lock_value:
                    try:
                        lock_data = json.loads(lock_value)
                        lock_time = datetime.fromisoformat(lock_data.get('timestamp', ''))
                        # If lock is older than 2 minutes, force release it
                        if (datetime.now(timezone.utc) - lock_time).total_seconds() > 120:
                            logger.warning(f"Found stale scaling lock (created {lock_time}), force-releasing")
                            self.database.redis.delete(lock_key)
                        else:
                            logger.info("Scaling operation already in progress, skipping cycle")
                            return False
                    except (json.JSONDecodeError, ValueError):
                        # If we can't parse the lock, just delete it
                        logger.warning("Invalid scaling lock format, deleting")
                        self.database.redis.delete(lock_key)
                else:
                    # Old format lock, just delete it
                    logger.warning("Found old format scaling lock, deleting")
                    self.database.redis.delete(lock_key)

            # Set lock with timestamp and 60s expiry to prevent infinite locks
            lock_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "count": count
            }
            self.database.redis.set(lock_key, json.dumps(lock_data), expire=60)
            logger.info(f"Acquired scaling operation lock for {action}")

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
        finally:
            # Always release the lock
            if hasattr(self.database, 'redis') and self.database.redis:
                lock_key = REDIS_KEYS['SCALING_LOCK']
                self.database.redis.delete(lock_key)
                logger.info("Released scaling operation lock")

    def _scale_up(self, count: int, decision: Dict[str, Any]) -> bool:
        """Scale up by adding worker nodes using async operations"""
        logger.info(f"Scaling up by {count} nodes (async mode)")
    # Defensive check: Verify counter is not corrupted
        try:
            if hasattr(self.database, 'redis') and self.database.redis:
                current_counter = int(self.database.redis.get("workers:next_number") or settings.autoscaler.worker_start_number)
                
                # Get all existing workers
                all_workers = self.database.get_all_workers()
                if all_workers:
                    max_existing = max(
                        int(w.node_name.split('-')[-1]) 
                        for w in all_workers 
                        if w.node_name.split('-')[-1].isdigit()
                    )
                    
                    if current_counter <= max_existing:
                        logger.warning(
                            f"Counter corruption detected: counter={current_counter}, "
                            f"max_existing={max_existing}. Fixing..."
                        )
                        self.database.redis.set("workers:next_number", max_existing + 1)
        except Exception as e:
            logger.warning(f"Could not verify counter: {e}")

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
                # If we're in an async context (FastAPI), we need to run in a thread
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
            if successful_workers:
                logger.info(f"Successfully scaled up by {len(successful_workers)} nodes: {successful_workers}")
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

            # Log scaling action details (instead of events)
            logger.info(f"Scale up details: +{len(successful_workers)} workers, "
                      f"Reason: {decision.get('reason', 'Resource pressure or pending pods')}")

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

        # Count permanent, removable, and configurable workers from Redis sets
        permanent_workers = 0
        removable_workers = 0
        configurable_workers = 0
        all_workers_count = 0

        # Try to get worker counts from Redis sets first
        if hasattr(self.database, 'redis') and self.database.redis:
            try:
                # Get counts from Redis sets
                permanent_workers = self.database.redis.scard(REDIS_KEYS['WORKERS_PERMANENT'])
                removable_workers = self.database.redis.scard(REDIS_KEYS['WORKERS_REMOVABLE'])
                all_workers_count = self.database.redis.scard(REDIS_KEYS['WORKERS_ALL'])

                # Calculate configurable workers (those that aren't permanent)
                configurable_workers = max(0, all_workers_count - permanent_workers)

                # Get actual lists for logging
                permanent_list = list(self.database.redis.smembers(REDIS_KEYS['WORKERS_PERMANENT']))
                removable_list = list(self.database.redis.smembers(REDIS_KEYS['WORKERS_REMOVABLE']))

                logger.info(f"Worker counts from Redis sets:")
                logger.info(f"  Permanent: {permanent_workers} {permanent_list}")
                logger.info(f"  Removable: {removable_workers} {removable_list}")
                logger.info(f"  Configurable: {configurable_workers}")

            except Exception as e:
                logger.warning(f"Failed to get worker counts from Redis sets: {e}")
                # Fallback to old JSON method
                try:
                    permanent_workers_list = self.database.redis.get("workers:permanent", deserialize=False)
                    if permanent_workers_list:
                        permanent_workers_list = json.loads(permanent_workers_list)
                        permanent_workers = len(permanent_workers_list)
                        logger.info(f"Permanent workers from Redis JSON: {permanent_workers_list}")
                except Exception as e2:
                    logger.warning(f"Failed to get permanent workers from Redis JSON: {e2}")

        # Fallback to database or Docker counts if Redis failed
        if permanent_workers == 0:
            if db_workers:
                permanent_workers = sum(1 for w in db_workers if w.node_name in settings.autoscaler.permanent_workers)
                removable_workers = sum(1 for w in db_workers if w.node_name not in settings.autoscaler.permanent_workers)
                configurable_workers = removable_workers
            elif actual_docker_nodes > 0:
                # Check Docker containers if DB is empty and Redis not available
                containers = [c for c in self.docker_client.containers.list(all=True)
                              if c.name and c.name.startswith(self.worker_prefix)]
                permanent_workers = sum(1 for c in containers
                                      if c.name in settings.autoscaler.permanent_workers)
                removable_workers = actual_docker_nodes - permanent_workers
                configurable_workers = removable_workers

        logger.info(f"Worker breakdown: permanent={permanent_workers}, removable={removable_workers}")

        # Calculate effective minimum: the greater of permanent workers or configured min_nodes
        # Add configurable workers as buffer for scaling
        effective_min = max(permanent_workers, min_nodes)

        # Log detailed breakdown
        logger.info(f"Node count breakdown:")
        logger.info(f"  Total nodes: {actual_nodes} (including master)")
        logger.info(f"  Worker nodes: {actual_nodes - 1} (excluding master)")
        logger.info(f"  Permanent workers: {permanent_workers}")
        logger.info(f"  Removable workers: {removable_workers}")
        logger.info(f"  Configurable workers: {configurable_workers}")
        logger.info(f"  Min nodes (config): {min_nodes}")
        logger.info(f"  Effective minimum: {effective_min}")

        # Can we scale down?
        can_scale_down = removable_workers > 0 and (actual_nodes - 1) > effective_min
        if can_scale_down:
            logger.info(f"Can scale down: {removable_workers} removable available")
        else:
            logger.info(f"Cannot scale down: need at least {effective_min} workers, have {actual_nodes - 1}")

        # Cannot scale down if we don't have removable workers
        if removable_workers <= 0:
            logger.info(f"Cannot scale down: no removable workers available (permanent={permanent_workers}, removable={removable_workers})")
            return False

        # Emergency stop: ensure we don't go below effective minimum
        if actual_nodes - count < effective_min:
            logger.error(f"EMERGENCY STOP: Cannot remove {count} nodes, would go below effective minimum (current={actual_nodes}, effective_min={effective_min})")
            logger.error("Manual intervention required - cluster at minimum capacity")
            return False

        # Get workers sorted for LIFO removal
        workers = self.database.get_all_workers()

        # Filter out permanent workers first
        removable_workers = [w for w in workers if w.node_name not in settings.autoscaler.permanent_workers]

        logger.info(f"Total workers: {len(workers)}, Removable workers: {len(removable_workers)}")
        if removable_workers:
            logger.info(f"Removable workers: {[w.node_name for w in removable_workers]}")

        # IMPROVED LIFO: Sort by worker number extracted from name (most reliable)
        def get_worker_number(worker):
            """Extract worker number from worker name for LIFO sorting"""
            try:
                # Extract number from name like "k3s-worker-5" -> 5
                return int(worker.node_name.split('-')[-1])
            except (ValueError, IndexError):
                # Fallback to timestamp if number extraction fails
                if worker.launched_at:
                    if worker.launched_at.tzinfo is None:
                        return worker.launched_at.replace(tzinfo=timezone.utc).timestamp()
                    return worker.launched_at.timestamp()
                return 0  # Unknown workers go first

        # Sort by worker number ascending (oldest numbers first)
        removable_workers.sort(key=get_worker_number)
        
        logger.info(f"Workers sorted for LIFO removal: {[f'{w.node_name}({get_worker_number(w)})' for w in removable_workers]}")

        # LIFO: Take the HIGHEST numbered workers (most recently created)
        # Since sorted ascending, take from the end
        workers_to_remove = removable_workers[-count:] if count < len(removable_workers) else removable_workers

        if workers_to_remove:
            logger.info(f"Workers selected for LIFO removal: {[w.node_name for w in workers_to_remove]}")
        else:
            logger.warning("No workers available for removal")
            return False
        
        # Sort workers by launch time (oldest first)
        # Handle None values and ensure consistent timezone awareness
        def safe_launched_at(worker):
            if worker.launched_at is None:
                # Use epoch time for workers without launch time
                return datetime(1970, 1, 1, tzinfo=timezone.utc)
            # Ensure timezone awareness
            if worker.launched_at.tzinfo is None:
                return worker.launched_at.replace(tzinfo=timezone.utc)
            return worker.launched_at

        workers.sort(key=safe_launched_at)

        # Filter out permanent workers (configured in settings)
        # These should never be removed
        removable_workers = [w for w in workers if w.node_name not in settings.autoscaler.permanent_workers]

        logger.info(f"Total workers: {len(workers)}, Removable workers: {len(removable_workers)}")
        if removable_workers:
            logger.info(f"Removable workers: {[w.node_name for w in removable_workers]}")

        # If database is empty but we have Docker containers, create temporary worker objects
        if not removable_workers and actual_docker_nodes > 0:
            logger.warning(f"Database has no removable workers but {actual_docker_nodes} Docker containers exist. Creating temporary worker objects.")
            containers = [c for c in self.docker_client.containers.list(all=True)
                          if c.name and c.name.startswith(self.worker_prefix)
                          and not c.name.endswith('-1') and not c.name.endswith('-2')]  # Skip permanent workers

            # Sort by creation time (newest first for LIFO)
            containers.sort(key=lambda c: c.attrs['Created'], reverse=True)

            # Create temporary WorkerNode objects for removal
            from database import WorkerNode, NodeStatus
            removable_workers = []
            for container in containers[:count]:
                worker = WorkerNode(
                    node_name=container.name,
                    container_id=container.id,
                    container_name=container.name,
                    status=NodeStatus.READY,  # Assume ready for removal
                    launched_at=datetime.now(timezone.utc),
                    metadata={"synced_from_docker": True}
                )
                removable_workers.append(worker)

        # LIFO: Select the most recently created workers (last in, first out)
        # Since workers are sorted oldest first, we take from the end
        workers_to_remove = removable_workers[-count:] if count < len(removable_workers) else removable_workers

        if workers_to_remove:
            logger.info(f"Workers selected for removal (LIFO): {[w.node_name for w in workers_to_remove]}")
        else:
            logger.warning("No workers available for removal")
            return False

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
                # loop = asyncio.get_running_loop()
                # If we're in an async context (FastAPI), we need to run in a thread
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

                # Log scaling action details (instead of events)
                logger.info(f"Scale down details: -{len(successful_removals)} workers, "
                          f"Reason: {decision.get('reason', 'Underutilization')}")

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

            # Use Redis atomic increment for worker numbering (prevents race conditions)
            from database.redis_client import AutoscalerRedisClient
            from config.settings import settings

            redis_client = AutoscalerRedisClient(
                host=settings.redis.host,
                port=settings.redis.port,
                db=settings.redis.cache_db,
                password=settings.redis.password
            )

            # Initialize counter if needed
            worker_counter_key = "workers:next_number"
            if not redis_client.exists(worker_counter_key):
                redis_client.set(worker_counter_key, settings.autoscaler.worker_start_number - 1)

            # Atomically get next worker number
            next_num = redis_client.increment(worker_counter_key, 1)
            node_name = f"{self.worker_prefix}-{next_num}"

            logger.info(f"Creating worker node: {node_name} (allocated from Redis counter)")

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

            # Delete removed workers from database
            for worker in db_workers:
                if worker.node_name not in actual_containers:
                    # Actually delete the worker, not just mark as removed
                    if self.database.workers.delete(worker.node_name):
                        logger.info(f"Deleted stale worker from database: {worker.node_name}")
                        # Also clear from Redis cache
                        if hasattr(self.database, 'cache') and self.database.cache:
                            self.database.cache.delete_worker(worker.node_name)
                    else:
                        # Fallback: just mark as removed
                        self.database.update_worker_status(worker.node_name, NodeStatus.REMOVED)
                        logger.warning(f"Could not delete worker, marked as REMOVED: {worker.node_name}")

            logger.info("Database sync with cluster completed")

        except Exception as e:
            logger.error(f"Failed to sync database with cluster: {e}")
            # Don't fail initialization, just log the error

    def _initialize_permanent_workers(self):
        """Initialize permanent workers in Redis"""
        logger.info(f"Initializing permanent workers in Redis: {settings.autoscaler.permanent_workers}")

        try:
            # Use Redis to store permanent worker information
            if hasattr(self.database, 'redis') and self.database.redis:
                # Store permanent workers list as a Redis set
                if settings.autoscaler.permanent_workers:
                    self.database.redis.set_add(REDIS_KEYS['WORKERS_PERMANENT'], *settings.autoscaler.permanent_workers)

                # Initialize worker counter to start after permanent workers
                # This ensures new workers start from the configured start number
                if not self.database.redis.exists(REDIS_KEYS['WORKER_COUNTER']):
                    self.database.redis.set(REDIS_KEYS['WORKER_COUNTER'], settings.autoscaler.worker_start_number)
                    logger.info(f"Initialized worker counter to start from {settings.autoscaler.worker_start_number}")

                # Mark each permanent worker
                for worker_name in settings.autoscaler.permanent_workers:
                    self.database.redis.hset(f"workers:{worker_name}", "permanent", "true")
                    self.database.redis.hset(f"workers:{worker_name}", "type", "permanent")
                    self.database.redis.hset(f"workers:{worker_name}", "created_at",
                                            datetime.now(timezone.utc).isoformat())

                logger.info(f"Permanent workers initialized in Redis: {settings.autoscaler.permanent_workers}")
                logger.info(f"Worker numbering will start from {settings.autoscaler.worker_start_number}")
            else:
                logger.warning("Redis not available, cannot store permanent workers")

        except Exception as e:
            logger.error(f"Failed to initialize permanent workers in Redis: {e}")

    