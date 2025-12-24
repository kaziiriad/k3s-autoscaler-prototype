#!/usr/bin/env python3
"""
Async scaling operations manager for concurrent worker node management
"""

import asyncio
import concurrent.futures
import json
import logging
import time
import docker
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from database import WorkerNode, NodeStatus
from config.settings import settings, REDIS_KEYS
from database.redis_client import AutoscalerRedisClient

logger = logging.getLogger(__name__)


class AsyncScalingManager:
    """
    Manages concurrent scaling operations using asyncio and thread pools
    """

    def __init__(self, config: Dict, max_workers: int = 3):
        """
        Initialize async scaling manager

        Args:
            config: Autoscaler configuration
            max_workers: Maximum number of concurrent operations
        """
        self.config = config
        self.max_workers = max_workers

        # Thread pool for blocking operations (Docker API calls)
        self.thread_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="async-scaling"
        )

        # Track operations in Redis instead of memory
        self.redis_client = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.cache_db,
            password=settings.redis.password
        )
        self.worker_counter_key = REDIS_KEYS['WORKER_COUNTER']
        logger.info(f"AsyncScalingManager initialized with max_workers={max_workers}")

    async def scale_up_concurrent(self, count: int, docker_client, database) -> Tuple[List[WorkerNode], List[str]]:
        """
        Scale up by creating multiple worker nodes concurrently

        Args:
            count: Number of nodes to create
            docker_client: Docker client instance
            database: Database manager instance

        Returns:
            Tuple of (successful_nodes, error_messages)
        """
        logger.info(f"Starting concurrent scale-up of {count} nodes")
        start_time = time.time()
        verification_timeout = getattr(settings.autoscaler, 'worker_verification_timeout', 240)

        # Create semaphore to limit concurrent Docker operations
        semaphore = asyncio.Semaphore(self.max_workers)

        async def _create_single_worker(worker_id: int) -> Tuple[Optional[WorkerNode], Optional[str]]:
            """Create a single worker node with semaphore control"""
            async with semaphore:
                try:
                    # Run blocking Docker operation in thread pool
                    loop = asyncio.get_event_loop()
                    worker = await loop.run_in_executor(
                        self.thread_pool,
                        self._create_worker_node_sync,
                        worker_id,
                        docker_client
                    )

                    if worker:
                        logger.info(f"Successfully created container: {worker.node_name}")
                        return worker, None
                    else:
                        return None, f"Failed to create worker-{worker_id}"

                except Exception as e:
                    error_msg = f"Error creating worker-{worker_id}: {str(e)}"
                    logger.error(error_msg)
                    return None, error_msg

        # Create all workers concurrently
        tasks = [_create_single_worker(i) for i in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        successful_workers = []
        error_messages = []

        for result in results:
            if isinstance(result, Exception):
                error_messages.append(f"Unexpected error: {str(result)}")
            else:
                worker, error = result
                if worker:
                    successful_workers.append(worker)
                if error:
                    error_messages.append(error)

        # Verify nodes with Kubernetes in parallel
        if successful_workers:
            logger.info(f"Verifying {len(successful_workers)} created nodes with Kubernetes")
            verification_tasks = []

            for worker in successful_workers:

                task = self._verify_node_with_kubernetes_async(worker, timeout=settings.autoscaler.worker_verification_timeout)
                verification_tasks.append(task)

            verification_results = await asyncio.gather(*verification_tasks, return_exceptions=True)

            # Update database with verified nodes
            database_updates = []
            for worker, verified in zip(successful_workers, verification_results):
                if isinstance(verified, dict) and verified.get('ready', False):
                    worker.status = NodeStatus.READY
                    worker.launched_at = datetime.now(timezone.utc)
                    database_updates.append(worker)
                else:
                    # Keep as INITIALIZING if verification failed/timed out
                    # The container exists, so we should track it
                    worker.status = NodeStatus.INITIALIZING
                    database_updates.append(worker)
                    logger.warning(f"Node {worker.node_name} verification timed out, but container exists. Keeping as INITIALIZING.")

            # Update database concurrently
            if database_updates:
                db_tasks = []
                for worker in database_updates:
                    task = asyncio.get_event_loop().run_in_executor(
                        self.thread_pool,
                        database.add_worker,
                        worker
                    )
                    db_tasks.append(task)

                db_results = await asyncio.gather(*db_tasks, return_exceptions=True)

                # Log any database errors
                for worker, result in zip(database_updates, db_results):
                    if isinstance(result, Exception):
                        error_messages.append(f"DB error for {worker.node_name}: {str(result)}")

        elapsed = time.time() - start_time
        logger.info(f"Concurrent scale-up completed in {elapsed:.2f}s: "
                   f"{len(successful_workers)} successful, {len(error_messages)} errors")

        return successful_workers, error_messages

    async def scale_down_concurrent(self, workers_to_remove: List[WorkerNode],
                                   docker_client, database) -> Tuple[List[WorkerNode], List[str]]:
        """
        Scale down by removing multiple worker nodes concurrently

        Args:
            workers_to_remove: List of worker nodes to remove
            docker_client: Docker client instance
            database: Database manager instance

        Returns:
            Tuple of (successful_removals, error_messages)
        """
        logger.info(f"Starting concurrent scale-down of {len(workers_to_remove)} nodes")
        start_time = time.time()

        # Drain nodes in parallel (if Kubernetes API available)
        if hasattr(self, 'k8s_api'):
            drain_tasks = []
            for worker in workers_to_remove:
                task = self._drain_node_async(worker)
                drain_tasks.append(task)

            drain_results = await asyncio.gather(*drain_tasks, return_exceptions=True)

            # Check drain results
            successful_drains = []
            for worker, result in zip(workers_to_remove, drain_results):
                if isinstance(result, Exception):
                    logger.warning(f"Drain failed for {worker.node_name}: {result}")
                else:
                    successful_drains.append(worker)
        else:
            successful_drains = workers_to_remove

        # Remove containers concurrently
        semaphore = asyncio.Semaphore(self.max_workers)

        async def _remove_single_worker(worker: WorkerNode) -> Tuple[bool, Optional[str]]:
            async with semaphore:
                try:
                    # Run blocking Docker operation in thread pool
                    loop = asyncio.get_event_loop()
                    success = await loop.run_in_executor(
                        self.thread_pool,
                        self._remove_worker_container_sync,
                        worker,
                        docker_client
                    )

                    if success:
                        logger.info(f"Successfully removed container: {worker.node_name}")
                        return True, None
                    else:
                        return False, f"Failed to remove {worker.node_name}"

                except Exception as e:
                    error_msg = f"Error removing {worker.node_name}: {str(e)}"
                    logger.error(error_msg)
                    return False, error_msg

        # Remove all workers concurrently
        removal_tasks = [_remove_single_worker(worker) for worker in successful_drains]
        removal_results = await asyncio.gather(*removal_tasks, return_exceptions=True)

        # Process results
        successful_removals = []
        error_messages = []

        for worker, result in zip(successful_drains, removal_results):
            if isinstance(result, Exception):
                error_messages.append(f"Unexpected error for {worker.node_name}: {str(result)}")
            else:
                success, error = result
                if success:
                    successful_removals.append(worker)
                if error:
                    error_messages.append(error)

        # Wait for Kubernetes nodes to be removed
        logger.debug(f"Scale-down check - successful_removals={len(successful_removals)}, has_k8s_api={hasattr(self, 'k8s_api')}")

        if successful_removals and hasattr(self, 'k8s_api'):
            logger.debug(f" Waiting for Kubernetes node removal for {len(successful_removals)} workers")
            wait_tasks = []
            for worker in successful_removals:
                logger.debug(f" Creating wait task for {worker.node_name}")
                task = self._wait_for_kubernetes_removal_async(worker)
                wait_tasks.append(task)

            wait_results = await asyncio.gather(*wait_tasks, return_exceptions=True)
            logger.debug(f" Wait results returned for {len(wait_results)} workers")

            # Update database with verified removals
            db_updates = []
            for worker, verified in zip(successful_removals, wait_results):
                logger.debug(f" Worker {worker.node_name} verified: {verified}")
                if isinstance(verified, dict) and verified.get('removed', False):
                    db_updates.append(worker)

            # Update database concurrently
            if db_updates:
                logger.debug(f" Calling database.remove_worker for {len(db_updates)} workers")
                db_tasks = []
                for worker in db_updates:
                    logger.debug(f" Scheduling DB removal for {worker.node_name}")
                    task = asyncio.get_event_loop().run_in_executor(
                        self.thread_pool,
                        database.remove_worker,
                        worker.node_name
                    )
                    db_tasks.append(task)

                db_results = await asyncio.gather(*db_tasks, return_exceptions=True)
                logger.debug(f" DB removal results: {len(db_results)}")

                # Log any database errors
                for worker, result in zip(db_updates, db_results):
                    if isinstance(result, Exception):
                        error_messages.append(f"DB removal error for {worker.node_name}: {str(result)}")
            else:
                logger.debug(" No workers verified as removed for database updates")
        else:
            if not successful_removals:
                logger.debug(" No successful removals to wait for")
            if not hasattr(self, 'k8s_api'):
                logger.debug("Kubernetes API not available, skipping node removal wait")

        elapsed = time.time() - start_time
        logger.info(f"Concurrent scale-down completed in {elapsed:.2f}s: "
                   f"{len(successful_removals)} successful, {len(error_messages)} errors")

        return successful_removals, error_messages

    async def _verify_node_with_kubernetes_async(self, worker: WorkerNode, timeout: int = 60) -> Dict[str, Any]:
        """Asynchronously verify a node with Kubernetes"""
        if not hasattr(self, 'k8s_api') or not self.k8s_api:
            return {"ready": True, "message": "Kubernetes API not available, assuming success"}

        start_time = time.time()
        last_error = None

        while (time.time() - start_time) < timeout:
            try:
                # Use thread pool for blocking Kubernetes API call
                loop = asyncio.get_event_loop()
                node = await loop.run_in_executor(
                    self.thread_pool,
                    self.k8s_api.read_node,
                    name=worker.node_name
                )

                # Check if node is ready
                if node.status.conditions:
                    for condition in node.status.conditions:
                        if condition.type == "Ready":
                            if condition.status == "True":
                                logger.info(f"Node {worker.node_name} is ready")
                                return {"ready": True, "message": "Node is ready"}
                            else:
                                last_error = f"Node Ready condition is {condition.status}: {condition.message or 'No message'}"

                await asyncio.sleep(5)  # Non-blocking sleep

            except Exception as e:
                last_error = str(e)
                if "404" in last_error or "Not Found" in last_error:
                    logger.warning(f"Node {worker.node_name} not found in Kubernetes API")
                    last_error = "Node not registered in Kubernetes"
                else:
                    logger.debug(f"Node {worker.node_name} not ready yet: {e}")
                await asyncio.sleep(5)

        # Get container logs for debugging
        try:
            import docker
            docker_client = docker.from_env()
            container = docker_client.containers.get(worker.container_name)
            container_logs = container.logs(tail=50).decode('utf-8')
            logger.error(f"Container logs for {worker.node_name}:\n{container_logs}")
        except Exception as log_error:
            logger.error(f"Could not get container logs: {log_error}")

        return {"ready": False, "message": f"Timeout waiting for node {worker.node_name}. Last error: {last_error}"}

    async def _drain_node_async(self, worker: WorkerNode) -> bool:
        """Asynchronously drain a Kubernetes node"""
        if not hasattr(self, 'k8s_api') or not self.k8s_api:
            return True  # Skip if Kubernetes API not available

        try:
            loop = asyncio.get_event_loop()

            # Mark as unschedulable
            await loop.run_in_executor(
                self.thread_pool,
                self._mark_node_unschedulable_sync,
                worker.node_name
            )

            # Evict pods (if needed)
            # This would be more complex in production

            logger.info(f"Successfully drained node: {worker.node_name}")
            return True

        except Exception as e:
            # Check if this is a 404 error (node doesn't exist)
            error_str = str(e)
            if "404" in error_str or "Not Found" in error_str:
                logger.info(f"Node {worker.node_name} not found in Kubernetes, treating as already drained")
                return True  # Node doesn't exist, so it's already "drained"
            else:
                logger.error(f"Failed to drain node {worker.node_name}: {e}")
                return False

    async def _wait_for_kubernetes_removal_async(self, worker: WorkerNode, timeout: int = 60) -> Dict[str, Any]:
        """Asynchronously wait for Kubernetes node removal"""
        if not hasattr(self, 'k8s_api') or not self.k8s_api:
            return {"removed": True, "message": "Kubernetes API not available"}

        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self.thread_pool,
                    self.k8s_api.read_node,
                    name=worker.node_name
                )
                # Node still exists, wait
                await asyncio.sleep(5)
            except Exception:
                # Node not found - good!
                return {"removed": True, "message": f"Node {worker.node_name} removed"}

        return {"removed": False, "message": f"Timeout waiting for {worker.node_name} removal"}

    def _create_worker_node_sync(self, worker_id: int, docker_client) -> Optional[WorkerNode]:
        """Synchronous worker creation (runs in thread pool)"""
        try:
            # CRITICAL FIX: Atomically increment FIRST to reserve the number
            # This prevents race conditions in concurrent scaling
            if not self.redis_client.exists(self.worker_counter_key):
                self.redis_client.set(self.worker_counter_key, settings.autoscaler.worker_start_number - 1)
            
            # Atomically get and increment - this reserves the number
            worker_num = int(self.redis_client.increment(self.worker_counter_key))
            node_name = f"{settings.autoscaler.worker_prefix}-{worker_num}"
            
            logger.info(f"Reserved worker number {worker_num}, creating {node_name}")

            # Create the container
            try:
                # Pre-create the volume directory
                import os
                volume_path = f'/tmp/k3s-{node_name}'
                os.makedirs(volume_path, exist_ok=True)

                container = docker_client.containers.run(
                    image=settings.docker.image,
                    name=node_name,
                    detach=True,
                    privileged=True,
                    hostname=node_name,
                    environment={
                        'K3S_URL': f"https://{settings.kubernetes.server_host or 'k3s-master'}:6443",
                        'K3S_TOKEN': settings.autoscaler.k3s_token,
                        'K3S_NODE_NAME': node_name,
                        'K3S_WITH_NODE_ID': 'true'
                    },
                    volumes={
                        volume_path: {'bind': '/var/lib/rancher/k3s', 'mode': 'rw'},
                    },
                    network=settings.docker.network,
                    restart_policy={'Name': 'always'},
                    labels={'com.docker.compose.project': 'prototype'},
                    command='agent',
                    healthcheck={
                        'test': ['CMD', 'pgrep', '-f', 'k3s'],
                        'interval': 10000000000,  # 10 seconds in nanoseconds
                        'timeout': 5000000000,    # 5 seconds
                        'retries': 3
                    }
                )
                
                logger.info(f"Successfully created worker {node_name} with reserved number {worker_num}")
                
            except Exception as e:
                logger.error(f"Failed to create container for {node_name}: {e}")
                # Don't decrement counter - skip this number to avoid reuse
                logger.warning(f"Worker number {worker_num} will be skipped due to creation failure")
                return None

            # Add worker to Redis sets
            self.redis_client.set_add(REDIS_KEYS['WORKERS_ALL'], node_name)
            self.redis_client.set_add(REDIS_KEYS['WORKERS_REMOVABLE'], node_name)

            worker_node = WorkerNode(
                node_name=node_name,
                container_id=container.id,
                container_name=node_name,
                status=NodeStatus.INITIALIZING,
                launched_at=datetime.now(timezone.utc),
                metadata={
                    "created_by": "autoscaler",
                    "container_image": settings.docker.image,
                    "network": settings.docker.network,
                    "worker_id": worker_id,
                    "worker_number": worker_num  # Store the number for reference
                }
            )

            return worker_node

        except Exception as e:
            logger.error(f"Failed to create worker node: {e}")
            return None
        
    def _remove_worker_container_sync(self, worker: WorkerNode, docker_client) -> bool:
        """Synchronous container removal (runs in thread pool)"""
        try:
            # Check if worker is permanent before removal
            permanent_workers = self.redis_client.set_members(REDIS_KEYS['WORKERS_PERMANENT'])
            if worker.node_name in permanent_workers:
                logger.info(f"Worker {worker.node_name} is marked as permanent; skipping removal.")
                return False

            # Remove the container
            container = docker_client.containers.get(worker.container_id)
            container.remove(force=True)
            logger.info(f"Successfully removed container: {worker.node_name}")

            # Update Redis sets - remove from all workers and removable sets
            self.redis_client.set_remove(REDIS_KEYS['WORKERS_ALL'], worker.node_name)
            self.redis_client.set_remove(REDIS_KEYS['WORKERS_REMOVABLE'], worker.node_name)

            # Also remove the individual worker hash entry
            worker_hash_key = f"worker:{worker.node_name}"
            self.redis_client.delete(worker_hash_key)
            logger.debug(f"Deleted worker hash entry: {worker_hash_key}")

            # Note: We don't decrement the worker counter as it should always increase
            # to ensure unique worker numbers even after removals

            return True
        except docker.errors.NotFound:
            logger.warning(f"Container not found for {worker.node_name}, treating as successful removal")
            # Still update Redis sets to clean up
            self.redis_client.set_remove(REDIS_KEYS['WORKERS_ALL'], worker.node_name)
            self.redis_client.set_remove(REDIS_KEYS['WORKERS_REMOVABLE'], worker.node_name)

            # Also remove the individual worker hash entry
            worker_hash_key = f"worker:{worker.node_name}"
            self.redis_client.delete(worker_hash_key)
            logger.debug(f"Deleted worker hash entry: {worker_hash_key}")

            return True
        except Exception as e:
            logger.error(f"Failed to remove container {worker.node_name}: {e}")
            return False
 
    def _mark_node_unschedulable_sync(self, node_name: str) -> None:
        """Synchronous node marking (runs in thread pool)"""
        if not hasattr(self, 'k8s_api') or not self.k8s_api:
            return

        try:
            patch_body = {"spec": {"unschedulable": True}}
            self.k8s_api.patch_node(name=node_name, body=patch_body)
        except Exception as e:
            # Re-raise the exception so the caller can handle it appropriately
            # This allows _drain_node_async to distinguish between different error types
            raise e

    def set_kubernetes_api(self, k8s_api):
        """Set Kubernetes API client for async operations"""
        self.k8s_api = k8s_api

    def _add_active_operation(self, operation_id: str, operation_data: dict):
        """Add an active operation to Redis"""
        self.redis_client.hset(REDIS_KEYS['SCALING_OPERATIONS'], operation_id, json.dumps(operation_data))
        self.redis_client.expire(REDIS_KEYS['SCALING_OPERATIONS'], 3600)  # Expire after 1 hour

    def _remove_active_operation(self, operation_id: str):
        """Remove an active operation from Redis"""
        self.redis_client.hdel(REDIS_KEYS['SCALING_OPERATIONS'], operation_id)

    def _get_active_operations(self) -> dict:
        """Get all active operations from Redis"""
        operations = {}
        for op_id, op_data in self.redis_client.hgetall(REDIS_KEYS['SCALING_OPERATIONS']).items():
            operations[op_id.decode()] = json.loads(op_data.decode())
        return operations

    def _add_operation_history(self, operation_data: dict):
        """Add operation to history in Redis"""
        history_key = REDIS_KEYS['SCALING_HISTORY']
        self.redis_client.lpush(history_key, json.dumps(operation_data))
        self.redis_client.ltrim(history_key, 0, 999)  # Keep only 1000 most recent operations

    def _get_operation_history(self, limit: int = 100) -> list:
        """Get operation history from Redis"""
        history = []
        for record in self.redis_client.lrange(REDIS_KEYS['SCALING_HISTORY'], 0, limit - 1):
            history.append(json.loads(record.decode()))
        return history

    async def cleanup(self):
        """Cleanup resources"""
        logger.info("Cleaning up AsyncScalingManager")
        self.thread_pool.shutdown(wait=True)