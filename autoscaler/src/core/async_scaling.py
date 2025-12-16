#!/usr/bin/env python3
"""
Async scaling operations manager for concurrent worker node management
"""

import asyncio
import concurrent.futures
import logging
import time
import os
import docker
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone
from database import WorkerNode, NodeStatus
from config.settings import settings

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

        # Track operations
        self.active_operations = {}
        self.operation_history = []

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
                task = self._verify_node_with_kubernetes_async(worker, timeout=120)
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
        if successful_removals and hasattr(self, 'k8s_api'):
            wait_tasks = []
            for worker in successful_removals:
                task = self._wait_for_kubernetes_removal_async(worker)
                wait_tasks.append(task)

            wait_results = await asyncio.gather(*wait_tasks, return_exceptions=True)

            # Update database with verified removals
            db_updates = []
            for worker, verified in zip(successful_removals, wait_results):
                if isinstance(verified, dict) and verified.get('removed', False):
                    db_updates.append(worker)

            # Update database concurrently
            if db_updates:
                db_tasks = []
                for worker in db_updates:
                    task = asyncio.get_event_loop().run_in_executor(
                        self.thread_pool,
                        database.remove_worker,
                        worker.node_name
                    )
                    db_tasks.append(task)

                db_results = await asyncio.gather(*db_tasks, return_exceptions=True)

                # Log any database errors
                for worker, result in zip(db_updates, db_results):
                    if isinstance(result, Exception):
                        error_messages.append(f"DB removal error for {worker.node_name}: {str(result)}")

        elapsed = time.time() - start_time
        logger.info(f"Concurrent scale-down completed in {elapsed:.2f}s: "
                   f"{len(successful_removals)} successful, {len(error_messages)} errors")

        return successful_removals, error_messages

    async def _verify_node_with_kubernetes_async(self, worker: WorkerNode, timeout: int = 60) -> Dict[str, Any]:
        """Asynchronously verify a node with Kubernetes"""
        if not hasattr(self, 'k8s_api') or not self.k8s_api:
            return {"ready": True, "message": "Kubernetes API not available, assuming success"}

        start_time = time.time()

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
                        if condition.type == "Ready" and condition.status == "True":
                            return {"ready": True, "message": "Node is ready"}

                await asyncio.sleep(5)  # Non-blocking sleep

            except Exception as e:
                logger.debug(f"Node {worker.node_name} not ready yet: {e}")
                await asyncio.sleep(5)

        return {"ready": False, "message": f"Timeout waiting for node {worker.node_name}"}

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
            # Find next available worker number
            existing_workers = []
            containers = docker_client.containers.list(all=True)
            for container in containers:
                if container.name and container.name.startswith("k3s-worker"):
                    try:
                        num = int(container.name.split('-')[-1])
                        existing_workers.append(num)
                    except:
                        pass

            next_num = max(existing_workers) + 1 if existing_workers else 3
            node_name = f"k3s-worker-{next_num}"

            # Create the container
            container = docker_client.containers.run(
                image=settings.docker.image,
                name=node_name,
                detach=True,
                privileged=True,
                environment={
                    'K3S_URL': f"https://{settings.kubernetes.server_host or 'k3s-master'}:6443",
                    'K3S_TOKEN': settings.autoscaler.k3s_token,
                    'K3S_NODE_NAME': node_name,
                    'K3S_WITH_NODE_ID': 'true'
                },
                volumes={
                    f'/tmp/k3s-{node_name}': {'bind': '/var/lib/rancher/k3s', 'mode': 'rw'},
                },
                network=settings.docker.network,
                restart_policy={'Name': 'always'}
            )

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
                    "worker_id": worker_id
                }
            )

            return worker_node

        except Exception as e:
            logger.error(f"Failed to create worker node: {e}")
            return None

    def _remove_worker_container_sync(self, worker: WorkerNode, docker_client) -> bool:
        """Synchronous container removal (runs in thread pool)"""
        try:
            container = docker_client.containers.get(worker.container_id)
            container.remove(force=True)
            return True
        except docker.errors.NotFound:
            logger.warning(f"Container not found for {worker.node_name}")
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
            logger.warning(f"Could not mark node {node_name} as unschedulable: {e}")

    def set_kubernetes_api(self, k8s_api):
        """Set Kubernetes API client for async operations"""
        self.k8s_api = k8s_api

    async def cleanup(self):
        """Cleanup resources"""
        logger.info("Cleaning up AsyncScalingManager")
        self.thread_pool.shutdown(wait=True)