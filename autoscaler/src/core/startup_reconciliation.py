#!/usr/bin/env python3
"""
Improved Startup Sequence with State Reconciliation
Ensures clean state on startup and prevents data integrity issues
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional
import docker

logger = logging.getLogger(__name__)

# Import models with proper path handling
try:
    from database import WorkerNode, NodeStatus
except ImportError:
    # Fallback for direct execution
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import WorkerNode, NodeStatus


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
        
        # Initialize Docker client
        if not hasattr(autoscaler, 'docker_client'):
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
            
            # Step 2: Identify permanent workers
            logger.info("\nStep 2: Identifying permanent workers...")
            permanent_workers = self._identify_permanent_workers(state)
            results["permanent_workers"] = list(permanent_workers)
            
            # Step 3: Clean stale database entries
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

        # Get Docker containers
        try:
            containers = self.docker_client.containers.list(all=True)
            for container in containers:
                if container.name and container.name.startswith(self.worker_prefix):
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
            # Use Redis cache to get all workers
            workers_dict = self.database.get_all_workers_dict()
            for worker_name, worker_data in workers_dict.items():
                state["redis_workers"][worker_name] = {
                    "status": worker_data.get("status"),
                    "container_id": worker_data.get("container_id"),
                    "node_name": worker_name
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
                    state["k8s_nodes"].add(node.metadata.name)
                logger.info(f"Found {len(state['k8s_nodes'])} Kubernetes nodes")
            except Exception as e:
                logger.warning(f"Could not get Kubernetes nodes: {e}")
        
        return state
    
    def _identify_permanent_workers(self, state: Dict) -> set:
        """
        Identify permanent workers based on configuration and actual state
        
        Permanent workers are:
        1. Explicitly configured in settings
        2. Worker-1 and worker-2 (by convention)
        3. Any workers that were created by docker-compose
        """
        permanent = set()
        
        # Add configured permanent workers
        config_permanent = self.config.get('autoscaler', {}).get('permanent_workers', [])
        if config_permanent:
            permanent.update(config_permanent)
        
        # Add conventional permanent workers (worker-1, worker-2)
        permanent.add(f"{self.worker_prefix}-1")
        permanent.add(f"{self.worker_prefix}-2")
        
        # Check metadata in Redis for workers created by docker-compose
        for worker_name, details in state["redis_workers"].items():
            # Check if this was created by docker-compose (in metadata)
            # This would require storing metadata, which we should do
            pass
        
        # Filter to only include workers that actually exist
        existing_permanent = {w for w in permanent if w in state["docker_containers"]}
        
        logger.info(f"Identified {len(existing_permanent)} permanent workers: {existing_permanent}")
        return existing_permanent
    
    def _clean_stale_entries(self, state: Dict, permanent_workers: set) -> list:
        """Clean stale Redis entries that don't have corresponding Docker containers"""
        actions = []

        docker_names = set(state["docker_containers"].keys())
        redis_names = set(state["redis_workers"].keys())

        # Find workers in Redis but not in Docker (excluding permanent workers)
        stale_workers = redis_names - docker_names - permanent_workers

        if stale_workers:
            logger.warning(f"Found {len(stale_workers)} stale Redis entries")

            for worker_name in stale_workers:
                try:
                    logger.info(f"Removing stale Redis entry: {worker_name}")
                    if self.database.remove_worker(worker_name):
                        actions.append(f"Removed stale Redis entry: {worker_name}")
                        logger.info(f"✓ Removed from Redis: {worker_name}")
                except Exception as e:
                    logger.error(f"Failed to remove {worker_name}: {e}")
        else:
            logger.info("No stale Redis entries found")

        return actions
    
    def _sync_docker_to_database(self, state: Dict, permanent_workers: set) -> list:
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

                    # Add to Redis cache
                    from database import WorkerNode, NodeStatus
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

                    # Add to Redis (state store)
                    self.database.add_worker_to_cache(worker)

                    # Record in MongoDB as historical event only
                    self.database.store_scaling_event(
                        event_type="worker_discovered",
                        count=1,
                        reason=f"Worker {worker_name} discovered during startup reconciliation",
                        metadata={
                            "worker_name": worker_name,
                            "container_id": docker_details["id"],
                            "is_permanent": is_permanent,
                            "discovered_by": "startup_reconciliation"
                        }
                    )

                    actions.append(f"Added to Redis: {worker_name}")
                    logger.info(f"✓ Added to Redis: {worker_name}")
                except Exception as e:
                    logger.error(f"Failed to add {worker_name} to Redis: {e}")
        else:
            logger.info("All Docker containers are tracked in Redis")

        return actions
    
    def _fix_worker_counter(self, state: Dict) -> Optional[str]:
        """Fix the worker counter in Redis - ensure it NEVER decreases

        Only considers Docker containers and Redis (source of truth).
        """
        try:
            # Get highest worker number from Docker
            max_num = 0
            for worker_name in state["docker_containers"].keys():
                try:
                    num = int(worker_name.split('-')[-1])
                    max_num = max(max_num, num)
                except (ValueError, IndexError):
                    pass

            # Also check Redis for potentially higher numbers
            for worker_name in state["redis_workers"].keys():
                try:
                    num = int(worker_name.split('-')[-1])
                    max_num = max(max_num, num)
                except (ValueError, IndexError):
                    pass

            # Counter should be at least max_num + 1
            correct_counter = max_num + 1

            # Get current counter from Redis
            current_counter = int(self.database.redis.get("workers:next_number") or 0)

            # CRITICAL: Counter should NEVER decrease
            if current_counter < correct_counter:
                logger.warning(
                    f"Worker counter is too low: {current_counter} "
                    f"(should be at least {correct_counter} based on existing workers)"
                )
                self.database.redis.set("workers:next_number", correct_counter)
                logger.info(f"✓ Fixed worker counter: {current_counter} → {correct_counter}")
                return f"Fixed worker counter: {current_counter} → {correct_counter}"
            elif current_counter > correct_counter:
                logger.info(
                    f"Worker counter is ahead of existing workers: {current_counter} > {correct_counter}. "
                    f"This is normal if workers were created and removed. Keeping current value."
                )
                return None
            else:
                logger.info(f"Worker counter is correct: {current_counter}")
                return None

        except Exception as e:
            logger.error(f"Failed to fix worker counter: {e}")
            return None
    
    def _verify_kubernetes_nodes(self, state: Dict, permanent_workers: set) -> list:
        """Verify Kubernetes nodes and remove orphaned ones"""
        actions = []
        
        if not hasattr(self.autoscaler.metrics, 'k8s_api') or not self.autoscaler.metrics.k8s_api:
            logger.info("Kubernetes API not available, skipping node verification")
            return actions
        
        docker_names = set(state["docker_containers"].keys())
        k8s_names = state["k8s_nodes"]
        
        # Find orphaned Kubernetes nodes (in K8s but not in Docker)
        orphaned_k8s = k8s_names - docker_names - permanent_workers
        
        if orphaned_k8s:
            logger.warning(f"Found {len(orphaned_k8s)} orphaned Kubernetes nodes")
            
            for node_name in orphaned_k8s:
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
    
    def _update_permanent_workers_in_redis(self, permanent_workers: set):
        """Update permanent workers list in Redis"""
        import json
        
        try:
            self.database.redis.set(
                "workers:permanent",
                json.dumps(list(permanent_workers))
            )
            logger.info(f"✓ Updated permanent workers in Redis: {permanent_workers}")
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


# Add to K3sAutoscaler.__init__():
# 
# # After initializing components...
# 
# # Perform startup reconciliation
# from core.startup_reconciliation import initialize_autoscaler_with_reconciliation
# initialize_autoscaler_with_reconciliation(self)
