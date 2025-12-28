#!/usr/bin/env python3
"""
Improved Startup Sequence with State Reconciliation
Ensures clean state on startup and prevents data integrity issues

CRITICAL FIX: Permanent workers are ALWAYS protected, regardless of Docker state
"""

import logging
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Set
import docker

logger = logging.getLogger(__name__)

# Import models with proper path handling
try:
    from database import WorkerNode, NodeStatus
    from config.settings import REDIS_KEYS
except ImportError:
    # Fallback for direct execution
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import WorkerNode, NodeStatus
    from config.settings import REDIS_KEYS


class StartupReconciler:
    """
    Performs startup reconciliation to ensure clean state
    """

    def __init__(self, autoscaler):
        """Initialize startup reconciler"""
        self.autoscaler = autoscaler
        self.config = autoscaler.config
        self.database = autoscaler.database
        self.worker_prefix = autoscaler.worker_prefix
        self.dry_run = self.config.get('autoscaler', {}).get('dry_run', False)

        # Initialize Docker client (skip in dry-run mode)
        if self.dry_run:
            self.docker_client = None
            logger.info("Dry-run mode: Docker client disabled")
        elif not hasattr(autoscaler, 'docker_client'):
            self.docker_client = docker.from_env()
        else:
            self.docker_client = autoscaler.docker_client

    def reconcile_on_startup(self) -> Dict:
        """
        Perform startup reconciliation

        Returns:
            Dict with reconciliation results
        """
        logger.info("=" * 80)
        logger.info("STARTUP RECONCILIATION")
        logger.info("=" * 80)

        results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actions": [],
            "warnings": [],
            "errors": []
        }

        try:
            # Step 1: Identify all data sources
            logger.info("\nStep 1: Gathering current state...")
            state = self._gather_state()
            results["state"] = state

            # Step 2: Identify permanent workers (BEFORE cleaning!)
            logger.info("\nStep 2: Identifying permanent workers...")
            permanent_workers = self._identify_permanent_workers(state)
            results["permanent_workers"] = list(permanent_workers)

            # Step 3: Clean stale database entries (PROTECTED: excludes permanent workers)
            logger.info("\nStep 3: Cleaning stale database entries...")
            cleaned = self._clean_stale_entries(state, permanent_workers)
            results["actions"].extend(cleaned)

            # Step 4: Sync Docker containers to database
            logger.info("\nStep 4: Syncing Docker containers to database...")
            synced = self._sync_docker_to_database(state, permanent_workers)
            results["actions"].extend(synced)

            # Step 5: Fix worker counter
            logger.info("\nStep 5: Fixing worker counter...")
            counter_fixed = self._fix_worker_counter(state)
            if counter_fixed:
                results["actions"].append(counter_fixed)

            # Step 6: Verify Kubernetes nodes
            logger.info("\nStep 6: Verifying Kubernetes nodes...")
            k8s_actions = self._verify_kubernetes_nodes(state, permanent_workers)
            results["actions"].extend(k8s_actions)

            # Step 7: Update Redis with permanent workers
            logger.info("\nStep 7: Updating Redis with permanent workers...")
            self._update_permanent_workers_in_redis(permanent_workers)
            results["actions"].append("Updated permanent workers in Redis")

            logger.info("\n" + "=" * 80)
            logger.info("STARTUP RECONCILIATION COMPLETE")
            logger.info(f"Actions taken: {len(results['actions'])}")
            logger.info(f"Warnings: {len(results['warnings'])}")
            logger.info(f"Errors: {len(results['errors'])}")
            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"Startup reconciliation failed: {e}")
            results["errors"].append(str(e))

        return results

    def _gather_state(self) -> Dict:
        """Gather current state from all sources

        Redis is the source of truth for worker state.
        MongoDB is only for historical events (scaling history).
        """
        state = {
            "docker_containers": {},
            "redis_workers": {},
            "k8s_nodes": set()
        }

        # Get Docker containers (skip in dry-run mode)
        if self.dry_run:
            logger.info("Dry-run mode: Skipping Docker container discovery")
        else:
            try:
                containers = self.docker_client.containers.list(all=True)
                for container in containers:
                    if container.name and self._is_worker_name(container.name):
                        state["docker_containers"][container.name] = {
                            "id": container.id[:12],
                            "status": container.status,
                            "running": container.status == "running"
                        }
                logger.info(f"Found {len(state['docker_containers'])} Docker containers")
            except Exception as e:
                logger.error(f"Failed to get Docker containers: {e}")

        # Get Redis workers (source of truth for worker state)
        try:
            # Get all worker hash keys from Redis
            # Worker hashes are stored as "workers:{worker_name}"
            # We need to filter out non-worker keys like "permanent" and "next_number"
            all_keys = self.database.redis.get_all_keys("workers:*")
            worker_keys = []
            for key in all_keys:
                # Skip special keys that are not worker hashes
                # Worker keys are like "workers:k3s-worker-N", not "workers:permanent" or "workers:next_number"
                key_name = key.replace("workers:", "")
                if self._is_worker_name(key_name):
                    worker_keys.append(key)

            for key in worker_keys:
                # Extract worker name from key (remove "workers:" prefix)
                worker_name = key.replace("workers:", "")
                # Get worker hash data
                worker_data = self.database.redis.hgetall(key)
                state["redis_workers"][worker_name] = {
                    "status": worker_data.get("status"),
                    "container_id": worker_data.get("container_id"),
                    "node_name": worker_name,
                    "permanent": worker_data.get("permanent") == "true"
                }
            logger.info(f"Found {len(state['redis_workers'])} Redis workers")
        except Exception as e:
            logger.warning(f"Could not get Redis workers: {e}")

        # Get Kubernetes nodes
        if hasattr(self.autoscaler.metrics, 'k8s_api') and self.autoscaler.metrics.k8s_api:
            try:
                from kubernetes import client
                nodes = self.autoscaler.metrics.k8s_api.list_node()
                for node in nodes.items:
                    # Skip control plane
                    if node.metadata.labels and \
                       'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                        continue
                    if self._is_worker_name(node.metadata.name):
                        state["k8s_nodes"].add(node.metadata.name)
                logger.info(f"Found {len(state['k8s_nodes'])} Kubernetes nodes")
            except Exception as e:
                logger.warning(f"Could not get Kubernetes nodes: {e}")

        return state

    def _is_worker_name(self, name: str) -> bool:
        """Check if a name is a worker node/container name"""
        if not name:
            return False
        # Worker names are like "k3s-worker-1", "k3s-worker-2", etc.
        pattern = f"^{re.escape(self.worker_prefix)}-\\d+$"
        return bool(re.match(pattern, name))

    def _identify_permanent_workers(self, state: Dict) -> Set[str]:
        """
        Identify permanent workers based on configuration

        CRITICAL: Permanent workers are ALWAYS protected, regardless of Docker state!
        They should never be removed during reconciliation, even if containers are stopped/missing.

        Permanent workers are identified by:
        1. Explicitly configured in settings (config.autoscaler.permanent_workers)
        2. Worker-1 and Worker-2 by convention (if not in config)
        3. Workers marked as permanent in Redis metadata
        """
        permanent = set()

        # 1. Add explicitly configured permanent workers
        config_permanent = self.config.get('autoscaler', {}).get('permanent_workers', [])
        if config_permanent:
            permanent.update(config_permanent)
            logger.info(f"Config permanent workers: {config_permanent}")

        # 2. Add conventional permanent workers (worker-1 and worker-2) if not already configured
        for num in [1, 2]:
            conventional_permanent = f"{self.worker_prefix}-{num}"
            if conventional_permanent not in permanent:
                permanent.add(conventional_permanent)
                logger.info(f"Added conventional permanent worker: {conventional_permanent}")

        # 3. Add workers marked as permanent in Redis
        for worker_name, details in state["redis_workers"].items():
            if details.get("permanent"):
                permanent.add(worker_name)
                logger.info(f"Found Redis-marked permanent worker: {worker_name}")

        # IMPORTANT: Do NOT filter by Docker existence!
        # Permanent workers must be protected even if:
        # - Docker containers are stopped
        # - Docker is not running
        # - Containers are being restarted
        # - In dry-run mode

        logger.info(f"PROTECTED: {len(permanent)} permanent workers will never be removed: {permanent}")
        return permanent

    def _clean_stale_entries(self, state: Dict, permanent_workers: Set[str]) -> list:
        """
        Clean stale Redis entries that don't have corresponding Docker containers

        CRITICAL SAFETY: Permanent workers are NEVER removed, even if Docker containers missing!
        """
        actions = []

        docker_names = set(state["docker_containers"].keys())
        redis_names = set(state["redis_workers"].keys())

        # Find workers in Redis but not in Docker (excluding permanent workers)
        stale_workers = redis_names - docker_names - permanent_workers

        if stale_workers:
            logger.warning(f"Found {len(stale_workers)} stale Redis entries (excluding {len(permanent_workers)} permanent)")
            logger.info(f"Stale workers to remove: {stale_workers}")
            logger.info(f"Protected permanent workers: {permanent_workers}")

            for worker_name in stale_workers:
                # Double-check this is not a permanent worker (paranoid safety check)
                if worker_name in permanent_workers:
                    logger.error(f"SAFETY CHECK FAILED: Attempted to remove permanent worker {worker_name}! Skipping.")
                    continue

                try:
                    logger.info(f"Removing stale Redis entry: {worker_name}")
                    if self.database.remove_worker(worker_name):
                        actions.append(f"Removed stale Redis entry: {worker_name}")
                        logger.info(f"✓ Removed from Redis: {worker_name}")
                except Exception as e:
                    logger.error(f"Failed to remove {worker_name}: {e}")
        else:
            logger.info("No stale Redis entries found (all Redis workers exist in Docker or are permanent)")

        return actions

    def _sync_docker_to_database(self, state: Dict, permanent_workers: Set[str]) -> list:
        """Sync Docker containers to Redis (source of truth for worker state)

        Note: MongoDB is only for historical events (scaling history), not worker state.
        """
        actions = []

        docker_names = set(state["docker_containers"].keys())
        redis_names = set(state["redis_workers"].keys())

        # Find workers in Docker but not in Redis
        missing_in_redis = docker_names - redis_names

        if missing_in_redis:
            logger.warning(f"Found {len(missing_in_redis)} Docker containers not in Redis")

            for worker_name in missing_in_redis:
                try:
                    docker_details = state["docker_containers"][worker_name]

                    # Determine if this is a permanent worker
                    is_permanent = worker_name in permanent_workers

                    # Add to Redis cache (not MongoDB - that's for historical events)
                    worker = WorkerNode(
                        node_name=worker_name,
                        container_id=docker_details["id"],
                        container_name=worker_name,
                        status=NodeStatus.READY if docker_details["running"] else NodeStatus.STOPPED,
                        launched_at=datetime.now(timezone.utc),
                        metadata={
                            "created_by": "docker_compose" if is_permanent else "startup_reconciliation",
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                            "is_permanent": is_permanent
                        }
                    )

                    # Add to Redis cache (state store)
                    self.database.add_worker_to_cache(worker)

                    # Also add to workers:all set
                    self.database.redis.set_add(REDIS_KEYS['WORKERS_ALL'], worker_name)

                    # If permanent, add to permanent set and mark metadata
                    if is_permanent:
                        self.database.redis.set_add(REDIS_KEYS['WORKERS_PERMANENT'], worker_name)

                    actions.append(f"Added to Redis: {worker_name} (permanent={is_permanent})")
                    logger.info(f"✓ Added to Redis: {worker_name}")
                except Exception as e:
                    logger.error(f"Failed to add {worker_name} to Redis: {e}")
        else:
            logger.info("All Docker containers are tracked in Redis")

        # Verify permanent workers are in Redis (even if not in Docker)
        for perm_worker in permanent_workers:
            if perm_worker not in redis_names and perm_worker not in docker_names:
                logger.warning(
                    f"Permanent worker {perm_worker} not found in Redis or Docker. "
                    f"This is OK if it's being started for the first time."
                )

        return actions

    def _fix_worker_counter(self, state: Dict) -> Optional[str]:
        """
        Fix the worker counter in Redis using the Redis client's helper method.

        Redis is the single source of truth for worker numbering.
        This method ensures counter NEVER decreases below the maximum existing worker number.

        Counter logic:
        - Counter = max(all_worker_numbers) + 1
        - Counter ONLY goes up, never down (except immediate rollback)
        - Gaps in numbering are normal and expected after scale-downs
        """
        try:
            # Get highest worker number from ALL sources (Docker + Redis)
            max_num = 0
            docker_workers = list(state["docker_containers"].keys())
            redis_workers = list(state["redis_workers"].keys())

            logger.debug(f"Fixing counter - Docker workers: {docker_workers}")
            logger.debug(f"Fixing counter - Redis workers: {redis_workers}")

            # Check Docker workers
            for worker_name in docker_workers:
                try:
                    num = int(worker_name.split('-')[-1])
                    max_num = max(max_num, num)
                    logger.debug(f"  Docker worker {worker_name} -> number {num}")
                except (ValueError, IndexError):
                    pass

            # Check Redis workers
            for worker_name in redis_workers:
                try:
                    num = int(worker_name.split('-')[-1])
                    max_num = max(max_num, num)
                    logger.debug(f"  Redis worker {worker_name} -> number {num}")
                except (ValueError, IndexError):
                    pass

            # Counter should be at least max_num + 1
            required_counter = max_num + 1

            logger.info(f"Max worker number found: {max_num}, required counter: {required_counter}")

            # Get current counter from Redis
            current_counter = self.database.redis.get_next_worker_number()

            # Apply counter monotonicity rule
            if current_counter < required_counter:
                # Counter too low - FIX IT
                logger.warning(
                    f"Counter too low: {current_counter} < {required_counter}. "
                    f"Fixing to ensure next worker gets unique number."
                )
                self.database.redis.set_worker_counter(required_counter)
                logger.info(f"✓ Fixed worker counter: {current_counter} → {required_counter}")
                return f"Fixed worker counter: {current_counter} → {required_counter}"

            elif current_counter > required_counter:
                # Counter ahead of workers - THIS IS NORMAL
                logger.info(
                    f"✓ Counter ahead of workers: {current_counter} > {required_counter}. "
                    f"This is NORMAL after scale-downs or failed worker creations. "
                    f"Workers {required_counter}-{current_counter-1} were removed or never created."
                )
                return None

            else:
                # Counter exactly right
                logger.info(f"✓ Worker counter is correct: {current_counter}")
                return None

        except Exception as e:
            logger.error(f"Failed to fix worker counter: {e}")
            return None

    def _verify_kubernetes_nodes(self, state: Dict, permanent_workers: Set[str]) -> list:
        """
        Verify Kubernetes nodes and remove orphaned ones

        CRITICAL: Permanent workers are protected - never removed from K8s
        """
        actions = []

        if not hasattr(self.autoscaler.metrics, 'k8s_api') or not self.autoscaler.metrics.k8s_api:
            logger.info("Kubernetes API not available, skipping node verification")
            return actions

        docker_names = set(state["docker_containers"].keys())
        k8s_names = state["k8s_nodes"]

        # Find orphaned Kubernetes nodes (in K8s but not in Docker)
        # CRITICAL: Exclude permanent workers!
        orphaned_k8s = k8s_names - docker_names - permanent_workers

        if orphaned_k8s:
            logger.warning(f"Found {len(orphaned_k8s)} orphaned Kubernetes nodes (excluding permanent)")
            logger.info(f"Orphaned nodes to remove: {orphaned_k8s}")
            logger.info(f"Protected permanent workers: {permanent_workers}")

            for node_name in orphaned_k8s:
                # Double-check this is not a permanent worker (paranoid safety check)
                if node_name in permanent_workers:
                    logger.error(f"SAFETY CHECK FAILED: Attempted to remove permanent K8s node {node_name}! Skipping.")
                    continue

                try:
                    logger.info(f"Removing orphaned Kubernetes node: {node_name}")
                    from kubernetes import client
                    self.autoscaler.metrics.k8s_api.delete_node(
                        name=node_name,
                        body=client.V1DeleteOptions(
                            grace_period_seconds=0,
                            propagation_policy='Background'
                        )
                    )
                    actions.append(f"Removed orphaned K8s node: {node_name}")
                    logger.info(f"✓ Removed: {node_name}")
                except Exception as e:
                    logger.error(f"Failed to remove K8s node {node_name}: {e}")
        else:
            logger.info("No orphaned Kubernetes nodes found")

        return actions

    def _update_permanent_workers_in_redis(self, permanent_workers: Set[str]):
        """
        Update permanent workers in Redis Sets

        Uses Redis Sets for efficient membership checks:
        - workers:permanent - Set of permanent worker names
        """
        try:
            # Clear existing permanent workers set
            permanent_key = REDIS_KEYS.get('WORKERS_PERMANENT', 'workers:permanent')
            self.database.redis.delete(permanent_key)

            # Add all permanent workers to set
            if permanent_workers:
                self.database.redis.set_add(permanent_key, *permanent_workers)

            # Also mark each worker individually in their hash
            for worker_name in permanent_workers:
                worker_hash_key = f"workers:{worker_name}"
                self.database.redis.hset(worker_hash_key, "permanent", "true")
                self.database.redis.hset(worker_hash_key, "protected", "true")

            logger.info(f"✓ Updated {len(permanent_workers)} permanent workers in Redis: {permanent_workers}")

        except Exception as e:
            logger.error(f"Failed to update permanent workers in Redis: {e}")


# Integration in autoscaler initialization
def initialize_autoscaler_with_reconciliation(autoscaler):
    """
    Initialize autoscaler with startup reconciliation

    Call this in K3sAutoscaler.__init__() after basic initialization
    """
    logger.info("Performing startup reconciliation...")

    startup_reconciler = StartupReconciler(autoscaler)
    results = startup_reconciler.reconcile_on_startup()

    # Log summary
    if results.get("errors"):
        logger.error(f"Startup reconciliation had {len(results['errors'])} errors")
        for error in results["errors"]:
            logger.error(f"  - {error}")

    if results.get("warnings"):
        logger.warning(f"Startup reconciliation had {len(results['warnings'])} warnings")

    logger.info(f"Startup reconciliation complete: {len(results.get('actions', []))} actions taken")

    return results
