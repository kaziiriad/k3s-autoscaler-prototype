#!/usr/bin/env python3
"""
Automatic State Reconciliation System
Continuously syncs state between Docker, Kubernetes, Redis, and MongoDB
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Set, List, Optional
import docker

logger = logging.getLogger(__name__)

# Import models with proper path handling
try:
    from database import DatabaseManager, WorkerNode, NodeStatus
    from kubernetes import client
except ImportError:
    # Fallback for direct execution
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from database import DatabaseManager, WorkerNode, NodeStatus
    try:
        from kubernetes import client
    except ImportError:
        client = None

# Define metrics at module level to avoid duplication
try:
    from prometheus_client import Counter, Histogram

    RECONCILIATION_CYCLES = Counter(
        'autoscaler_reconciliation_cycles_total',
        'Total reconciliation cycles'
    )
    RECONCILIATION_DURATION = Histogram(
        'autoscaler_reconciliation_duration_seconds',
        'Time taken for reconciliation'
    )
    RECONCILIATION_ISSUES_FIXED = Counter(
        'autoscaler_reconciliation_issues_fixed_total',
        'Issues fixed by reconciliation',
        ['issue_type']
    )
except ImportError:
    RECONCILIATION_CYCLES = None
    RECONCILIATION_DURATION = None
    RECONCILIATION_ISSUES_FIXED = None


class StateReconciler:
    """
    Continuously reconciles state across all data sources
    Ensures Docker, Kubernetes, Redis, and MongoDB are always in sync
    """
    
    def __init__(
        self,
        database: DatabaseManager,
        docker_client: docker.DockerClient,
        k8s_api: Optional[client.CoreV1Api],
        worker_prefix: str = "k3s-worker",
        permanent_workers: List[str] = None,
        reconcile_interval: int = 60  # seconds
    ):
        """
        Initialize state reconciler
        
        Args:
            database: Database manager instance
            docker_client: Docker client
            k8s_api: Kubernetes API client
            worker_prefix: Prefix for worker containers
            permanent_workers: List of permanent worker names
            reconcile_interval: How often to reconcile (seconds)
        """
        self.database = database
        self.docker_client = docker_client
        self.k8s_api = k8s_api
        self.worker_prefix = worker_prefix
        self.permanent_workers = set(permanent_workers or ["k3s-worker-1", "k3s-worker-2"])
        self.reconcile_interval = reconcile_interval
        self.running = False
        
        # Metrics
        self.reconciliation_count = 0
        self.last_reconciliation = None
        self.issues_fixed = {
            "stale_redis": 0,
            "stale_mongo": 0,
            "orphaned_k8s": 0,
            "missing_db": 0,
            "status_fixes": 0,
            "counter_fixes": 0
        }
        
        logger.info(
            f"StateReconciler initialized (interval={reconcile_interval}s, "
            f"permanent_workers={self.permanent_workers})"
        )
    
    async def start(self):
        """Start the reconciliation loop"""
        if self.running:
            logger.warning("StateReconciler already running")
            return
        
        self.running = True
        logger.info("Starting StateReconciler")
        
        # Run initial reconciliation immediately
        await self.reconcile()
        
        # Start periodic reconciliation
        while self.running:
            try:
                await asyncio.sleep(self.reconcile_interval)
                await self.reconcile()
            except Exception as e:
                logger.error(f"Error in reconciliation loop: {e}")
                await asyncio.sleep(5)  # Brief pause before retry
    
    async def stop(self):
        """Stop the reconciliation loop"""
        self.running = False
        logger.info("Stopping StateReconciler")
    
    async def reconcile(self):
        """Perform a full state reconciliation"""
        start_time = datetime.now(timezone.utc)
        self.reconciliation_count += 1

        logger.info(f"Starting reconciliation #{self.reconciliation_count}")

        try:
            # Check if metrics are available
            metrics_available = (
                RECONCILIATION_CYCLES is not None and
                RECONCILIATION_DURATION is not None and
                RECONCILIATION_ISSUES_FIXED is not None
            )

            # 1. Gather current state from all sources
            state = await self._gather_state()

            # 2. Detect inconsistencies
            inconsistencies = self._detect_inconsistencies(state)

            # 3. Fix inconsistencies
            if inconsistencies:
                await self._fix_inconsistencies(inconsistencies, state)

                # Update metrics for issues fixed
                if metrics_available:
                    for issue_type, count in [
                        ('stale_redis', len(inconsistencies.get('stale_redis', []))),
                        ('stale_mongo', len(inconsistencies.get('stale_mongo', []))),
                        ('orphaned_k8s', len(inconsistencies.get('orphaned_k8s', []))),
                        ('missing_db', len(inconsistencies.get('missing_db', []))),
                        ('status_fixes', len(inconsistencies.get('status_mismatches', [])))
                    ]:
                        if count > 0:
                            RECONCILIATION_ISSUES_FIXED.labels(issue_type=issue_type).inc(count)
            else:
                logger.debug("No inconsistencies detected - state is synchronized")

            # 4. Verify worker counter
            counter_fixed = await self._verify_worker_counter(state)
            if counter_fixed and metrics_available:
                RECONCILIATION_ISSUES_FIXED.labels(issue_type='counter_fixes').inc()

            # 5. Update metrics
            self.last_reconciliation = datetime.now(timezone.utc)

            # Increment reconciliation cycles counter
            if metrics_available:
                RECONCILIATION_CYCLES.inc()

            duration = (self.last_reconciliation - start_time).total_seconds()

            # Record duration histogram
            if metrics_available:
                RECONCILIATION_DURATION.observe(duration)

            logger.info(f"Reconciliation #{self.reconciliation_count} completed in {duration:.2f}s")

        except Exception as e:
            logger.error(f"Reconciliation failed: {e}")
    
    async def _gather_state(self) -> Dict:
        """Gather current state from all sources"""
        state = {
            "docker": set(),
            "k8s": set(),
            "redis": set(),
            "mongo": set(),
            "docker_details": {},
            "redis_details": {},
            "mongo_details": {}
        }
        
        # Get Docker workers
        containers = self.docker_client.containers.list(all=True)
        for container in containers:
            if container.name and container.name.startswith(self.worker_prefix):
                state["docker"].add(container.name)
                state["docker_details"][container.name] = {
                    "id": container.id[:12],
                    "status": container.status,
                    "running": container.status == "running"
                }
        
        # Get Kubernetes workers
        if self.k8s_api:
            try:
                nodes = self.k8s_api.list_node()
                for node in nodes.items:
                    # Skip control plane
                    if node.metadata.labels and \
                       'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                        continue
                    state["k8s"].add(node.metadata.name)
            except Exception as e:
                logger.warning(f"Could not get Kubernetes nodes: {e}")
        
        # Get Redis workers
        try:
            workers = self.database.get_all_workers()
            for worker in workers:
                state["redis"].add(worker.node_name)
                state["redis_details"][worker.node_name] = {
                    "status": worker.status.value,
                    "launched_at": worker.launched_at
                }
        except Exception as e:
            logger.warning(f"Could not get Redis workers: {e}")
        
        # Get MongoDB workers
        try:
            workers = self.database.get_all_workers()
            for worker in workers:
                state["mongo"].add(worker.node_name)
                state["mongo_details"][worker.node_name] = {
                    "status": worker.status.value,
                    "container_id": worker.container_id
                }
        except Exception as e:
            logger.warning(f"Could not get MongoDB workers: {e}")
        
        logger.debug(
            f"State gathered: Docker={len(state['docker'])}, "
            f"K8s={len(state['k8s'])}, "
            f"Redis={len(state['redis'])}, "
            f"MongoDB={len(state['mongo'])}"
        )
        
        return state
    
    def _detect_inconsistencies(self, state: Dict) -> Dict:
        """Detect inconsistencies between data sources"""
        inconsistencies = {
            "stale_redis": [],      # In Redis but not in Docker
            "stale_mongo": [],      # In MongoDB but not in Docker
            "orphaned_k8s": [],     # In K8s but not in Docker
            "missing_db": [],       # In Docker but not in DB
            "status_mismatches": [] # Wrong status in Redis/MongoDB
        }
        
        docker = state["docker"]
        k8s = state["k8s"]
        redis = state["redis"]
        mongo = state["mongo"]
        
        # Find stale entries (in DB but not in Docker)
        for worker_name in redis - docker:
            if worker_name not in self.permanent_workers:
                inconsistencies["stale_redis"].append(worker_name)
        
        for worker_name in mongo - docker:
            if worker_name not in self.permanent_workers:
                inconsistencies["stale_mongo"].append(worker_name)
        
        # Find orphaned K8s nodes
        for worker_name in k8s - docker:
            if worker_name not in self.permanent_workers:
                inconsistencies["orphaned_k8s"].append(worker_name)
        
        # Find missing DB entries
        for worker_name in docker - redis:
            inconsistencies["missing_db"].append(worker_name)
        
        # Check status mismatches
        for worker_name in docker & redis:  # Workers in both
            docker_details = state["docker_details"].get(worker_name, {})
            redis_details = state["redis_details"].get(worker_name, {})
            
            if docker_details.get("running") and redis_details.get("status") == "initializing":
                inconsistencies["status_mismatches"].append({
                    "worker": worker_name,
                    "docker_status": "running",
                    "redis_status": "initializing"
                })
        
        # Log detected inconsistencies
        if any(inconsistencies.values()):
            logger.warning("Inconsistencies detected:")
            for key, value in inconsistencies.items():
                if value:
                    logger.warning(f"  {key}: {len(value)} items")
        
        return inconsistencies
    
    async def _fix_inconsistencies(self, inconsistencies: Dict, state: Dict):
        """Fix detected inconsistencies"""
        logger.info("Fixing inconsistencies...")
        
        # 1. Remove stale Redis entries
        for worker_name in inconsistencies["stale_redis"]:
            logger.info(f"Removing stale Redis entry: {worker_name}")
            try:
                # Delete from database (which updates Redis)
                if self.database.workers.delete(worker_name):
                    self.issues_fixed["stale_redis"] += 1
                    logger.info(f"✓ Removed stale Redis entry: {worker_name}")
            except Exception as e:
                logger.error(f"Failed to remove Redis entry {worker_name}: {e}")
        
        # 2. Remove stale MongoDB entries
        for worker_name in inconsistencies["stale_mongo"]:
            logger.info(f"Removing stale MongoDB entry: {worker_name}")
            try:
                if self.database.workers.delete(worker_name):
                    self.issues_fixed["stale_mongo"] += 1
                    logger.info(f"✓ Removed stale MongoDB entry: {worker_name}")
            except Exception as e:
                logger.error(f"Failed to remove MongoDB entry {worker_name}: {e}")
        
        # 3. Remove orphaned Kubernetes nodes
        for worker_name in inconsistencies["orphaned_k8s"]:
            if not self.k8s_api:
                continue
            
            logger.info(f"Removing orphaned Kubernetes node: {worker_name}")
            try:
                self.k8s_api.delete_node(
                    name=worker_name,
                    body=client.V1DeleteOptions(
                        grace_period_seconds=0,
                        propagation_policy='Background'
                    )
                )
                self.issues_fixed["orphaned_k8s"] += 1
                logger.info(f"✓ Removed orphaned K8s node: {worker_name}")
            except Exception as e:
                logger.error(f"Failed to remove K8s node {worker_name}: {e}")
        
        # 4. Add missing DB entries
        for worker_name in inconsistencies["missing_db"]:
            logger.info(f"Adding missing DB entry: {worker_name}")
            try:
                docker_details = state["docker_details"].get(worker_name, {})
                
                worker = WorkerNode(
                    node_name=worker_name,
                    container_id=docker_details.get("id", "unknown"),
                    container_name=worker_name,
                    status=NodeStatus.READY if docker_details.get("running") else NodeStatus.STOPPED,
                    launched_at=datetime.now(timezone.utc),
                    metadata={
                        "created_by": "reconciliation",
                        "synced_at": datetime.now(timezone.utc).isoformat()
                    }
                )
                
                if self.database.add_worker(worker):
                    self.issues_fixed["missing_db"] += 1
                    logger.info(f"✓ Added missing DB entry: {worker_name}")
            except Exception as e:
                logger.error(f"Failed to add DB entry {worker_name}: {e}")
        
        # 5. Fix status mismatches
        for mismatch in inconsistencies["status_mismatches"]:
            worker_name = mismatch["worker"]
            logger.info(f"Fixing status mismatch: {worker_name}")
            try:
                if self.database.update_worker_status(worker_name, NodeStatus.READY):
                    self.issues_fixed["status_fixes"] += 1
                    logger.info(f"✓ Fixed status for: {worker_name}")
            except Exception as e:
                logger.error(f"Failed to fix status {worker_name}: {e}")
    
    async def _verify_worker_counter(self, state: Dict) -> bool:
        """Verify and fix worker counter. Returns True if counter was fixed."""
        try:
            # Get highest worker number from Docker
            max_num = 0
            for worker_name in state["docker"]:
                try:
                    num = int(worker_name.split('-')[-1])
                    max_num = max(max_num, num)
                except (ValueError, IndexError):
                    pass

            # Get Redis counter
            redis_counter = int(self.database.redis.get("workers:next_number") or 0)

            # Counter should be at least max_num + 1
            correct_counter = max_num + 1

            if redis_counter < correct_counter:
                logger.warning(
                    f"Worker counter is wrong: {redis_counter} "
                    f"(should be {correct_counter})"
                )
                self.database.redis.set("workers:next_number", correct_counter)
                self.issues_fixed["counter_fixes"] += 1
                logger.info(f"✓ Fixed worker counter: {correct_counter}")
                return True
            else:
                logger.debug(f"Worker counter is correct: {redis_counter}")
                return False

        except Exception as e:
            logger.error(f"Failed to verify worker counter: {e}")
            return False
    
    def get_status(self) -> Dict:
        """Get reconciliation status"""
        return {
            "running": self.running,
            "reconciliation_count": self.reconciliation_count,
            "last_reconciliation": self.last_reconciliation.isoformat() if self.last_reconciliation else None,
            "interval_seconds": self.reconcile_interval,
            "issues_fixed": self.issues_fixed.copy()
        }
    
    def reset_metrics(self):
        """Reset issue counters"""
        self.issues_fixed = {
            "stale_redis": 0,
            "stale_mongo": 0,
            "orphaned_k8s": 0,
            "missing_db": 0,
            "status_fixes": 0,
            "counter_fixes": 0
        }
        logger.info("Metrics reset")


# Integration with main autoscaler
class ReconciliationService:
    """Service wrapper for state reconciliation"""
    
    def __init__(self, autoscaler):
        """Initialize reconciliation service"""
        self.autoscaler = autoscaler
        self.reconciler = StateReconciler(
            database=autoscaler.database,
            docker_client=autoscaler.docker_client if hasattr(autoscaler, 'docker_client') else None,
            k8s_api=autoscaler.metrics.k8s_api if hasattr(autoscaler.metrics, 'k8s_api') else None,
            worker_prefix=autoscaler.worker_prefix,
            permanent_workers=getattr(autoscaler.config.get('autoscaler', {}), 'permanent_workers', []),
            reconcile_interval=60  # Check every minute
        )
        self.task = None
    
    async def start(self):
        """Start the reconciliation service"""
        if self.task:
            logger.warning("Reconciliation service already running")
            return
        
        logger.info("Starting reconciliation service")
        self.task = asyncio.create_task(self.reconciler.start())
    
    async def stop(self):
        """Stop the reconciliation service"""
        if not self.task:
            return
        
        logger.info("Stopping reconciliation service")
        await self.reconciler.stop()
        
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        
        self.task = None
    
    def get_status(self) -> Dict:
        """Get reconciliation status"""
        return self.reconciler.get_status()


# Usage in main.py:
# from core.reconciliation import ReconciliationService
#
# # Initialize
# reconciliation_service = ReconciliationService(autoscaler)
#
# # Start in background
# await reconciliation_service.start()
#
# # Add API endpoint to check status
# @app.get("/reconciliation/status")
# async def get_reconciliation_status():
#     return reconciliation_service.get_status()
