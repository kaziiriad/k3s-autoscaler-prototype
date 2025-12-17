#!/usr/bin/env python3
"""
Data Integrity Cleanup Script for K3s Autoscaler
Fixes inconsistencies between Docker, Kubernetes, Redis, and MongoDB
"""

import docker
import redis
import pymongo
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Set
from kubernetes import client, config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DataIntegrityFixer:
    """Fix data integrity issues across all data sources"""
    
    def __init__(
        self,
        docker_host: str = "unix:///var/run/docker.sock",
        redis_host: str = "localhost",
        redis_port: int = 6379,
        mongo_url: str = "mongodb://admin:password@localhost:27017/autoscaler?authSource=admin",
        kubeconfig_path: str = "./kubeconfig/kubeconfig",
        worker_prefix: str = "k3s-worker",
        permanent_workers: List[str] = None
    ):
        """Initialize the fixer"""
        self.docker_client = docker.DockerClient(base_url=docker_host)
        self.redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        self.mongo_client = pymongo.MongoClient(mongo_url)
        self.db = self.mongo_client['autoscaler']
        
        # Load Kubernetes config
        try:
            config.load_kube_config(config_file=kubeconfig_path)
            self.k8s_api = client.CoreV1Api()
        except Exception as e:
            logger.warning(f"Could not load Kubernetes config: {e}")
            self.k8s_api = None
        
        self.worker_prefix = worker_prefix
        self.permanent_workers = permanent_workers or ["k3s-worker-1", "k3s-worker-2"]
        
        logger.info(f"Initialized DataIntegrityFixer with permanent workers: {self.permanent_workers}")
    
    def diagnose(self) -> Dict:
        """Diagnose data integrity issues"""
        logger.info("=" * 80)
        logger.info("DIAGNOSING DATA INTEGRITY ISSUES")
        logger.info("=" * 80)
        
        issues = {
            "docker_workers": [],
            "k8s_workers": [],
            "redis_workers": [],
            "mongo_workers": [],
            "inconsistencies": [],
            "orphaned_nodes": [],
            "stale_redis_keys": [],
            "wrong_worker_counter": False
        }
        
        # 1. Get Docker workers
        docker_workers = self._get_docker_workers()
        issues["docker_workers"] = docker_workers
        logger.info(f"\n✓ Docker workers found: {len(docker_workers)}")
        for w in docker_workers:
            logger.info(f"  - {w['name']} (status: {w['status']})")
        
        # 2. Get Kubernetes workers
        if self.k8s_api:
            k8s_workers = self._get_k8s_workers()
            issues["k8s_workers"] = k8s_workers
            logger.info(f"\n✓ Kubernetes workers found: {len(k8s_workers)}")
            for w in k8s_workers:
                logger.info(f"  - {w['name']} (ready: {w['ready']})")
        else:
            logger.warning("\n⚠ Kubernetes API not available")
        
        # 3. Get Redis workers
        redis_workers = self._get_redis_workers()
        issues["redis_workers"] = redis_workers
        logger.info(f"\n✓ Redis workers found: {len(redis_workers)}")
        for w in redis_workers:
            logger.info(f"  - {w['name']} (status: {w['status']})")
        
        # 4. Get MongoDB workers
        mongo_workers = self._get_mongo_workers()
        issues["mongo_workers"] = mongo_workers
        logger.info(f"\n✓ MongoDB workers found: {len(mongo_workers)}")
        for w in mongo_workers:
            logger.info(f"  - {w['name']} (status: {w['status']})")
        
        # 5. Find inconsistencies
        docker_names = {w['name'] for w in docker_workers}
        redis_names = {w['name'] for w in redis_workers}
        mongo_names = {w['name'] for w in mongo_workers}
        k8s_names = {w['name'] for w in k8s_workers} if self.k8s_api else set()
        
        # Workers in Redis but not in Docker
        orphaned_redis = redis_names - docker_names
        if orphaned_redis:
            issues["stale_redis_keys"].extend(list(orphaned_redis))
            logger.warning(f"\n⚠ Stale Redis entries: {orphaned_redis}")
        
        # Workers in MongoDB but not in Docker
        orphaned_mongo = mongo_names - docker_names
        if orphaned_mongo:
            issues["inconsistencies"].append(f"MongoDB has workers not in Docker: {orphaned_mongo}")
            logger.warning(f"⚠ Stale MongoDB entries: {orphaned_mongo}")
        
        # Workers in Kubernetes but not in Docker
        if self.k8s_api:
            orphaned_k8s = k8s_names - docker_names
            if orphaned_k8s:
                issues["orphaned_nodes"].extend(list(orphaned_k8s))
                logger.warning(f"⚠ Orphaned Kubernetes nodes: {orphaned_k8s}")
        
        # Workers in Docker but not in Redis/MongoDB
        missing_in_redis = docker_names - redis_names
        if missing_in_redis:
            issues["inconsistencies"].append(f"Docker workers not in Redis: {missing_in_redis}")
            logger.warning(f"⚠ Docker workers not tracked in Redis: {missing_in_redis}")
        
        # 6. Check worker counter
        actual_max = self._get_max_worker_number(docker_names)
        redis_counter = self._get_redis_worker_counter()
        
        if redis_counter <= actual_max:
            issues["wrong_worker_counter"] = True
            logger.error(f"\n❌ Worker counter is WRONG:")
            logger.error(f"   Redis counter: {redis_counter}")
            logger.error(f"   Highest existing: {actual_max}")
            logger.error(f"   Next worker should be: {actual_max + 1}")
        else:
            logger.info(f"\n✓ Worker counter is correct: {redis_counter}")
        
        # 7. Check for workers with wrong status
        for redis_worker in redis_workers:
            name = redis_worker['name']
            status = redis_worker['status']
            
            # Find corresponding docker container
            docker_worker = next((w for w in docker_workers if w['name'] == name), None)
            
            if docker_worker:
                if docker_worker['status'] == 'running' and status == 'initializing':
                    issues["inconsistencies"].append(
                        f"{name}: Redis status '{status}' but container is running"
                    )
                    logger.warning(f"⚠ {name}: Status mismatch (Redis: {status}, Docker: running)")
        
        logger.info("\n" + "=" * 80)
        logger.info("DIAGNOSIS COMPLETE")
        logger.info("=" * 80)
        
        return issues
    
    def fix_all(self, dry_run: bool = False) -> Dict:
        """Fix all detected issues"""
        logger.info("\n" + "=" * 80)
        logger.info(f"FIXING DATA INTEGRITY ISSUES (dry_run={dry_run})")
        logger.info("=" * 80)
        
        issues = self.diagnose()
        fixes = {
            "redis_cleaned": 0,
            "mongo_cleaned": 0,
            "k8s_cleaned": 0,
            "counter_fixed": False,
            "status_updated": 0
        }
        
        # 1. Remove stale Redis entries
        for worker_name in issues["stale_redis_keys"]:
            if worker_name in self.permanent_workers:
                logger.info(f"Skipping permanent worker: {worker_name}")
                continue
            
            logger.info(f"Cleaning stale Redis entry: {worker_name}")
            if not dry_run:
                self._clean_redis_worker(worker_name)
                fixes["redis_cleaned"] += 1
        
        # 2. Remove stale MongoDB entries
        for worker in issues["mongo_workers"]:
            if worker['name'] not in {w['name'] for w in issues["docker_workers"]}:
                if worker['name'] in self.permanent_workers:
                    logger.info(f"Skipping permanent worker: {worker['name']}")
                    continue
                
                logger.info(f"Cleaning stale MongoDB entry: {worker['name']}")
                if not dry_run:
                    self._clean_mongo_worker(worker['name'])
                    fixes["mongo_cleaned"] += 1
        
        # 3. Remove orphaned Kubernetes nodes
        for node_name in issues["orphaned_nodes"]:
            if node_name in self.permanent_workers:
                logger.info(f"Skipping permanent worker: {node_name}")
                continue
            
            logger.info(f"Removing orphaned Kubernetes node: {node_name}")
            if not dry_run:
                self._remove_k8s_node(node_name)
                fixes["k8s_cleaned"] += 1
        
        # 4. Fix worker counter
        if issues["wrong_worker_counter"]:
            docker_names = {w['name'] for w in issues["docker_workers"]}
            correct_counter = self._get_max_worker_number(docker_names) + 1
            
            logger.info(f"Fixing worker counter: setting to {correct_counter}")
            if not dry_run:
                self._fix_worker_counter(correct_counter)
                fixes["counter_fixed"] = True
        
        # 5. Fix status mismatches
        for inconsistency in issues["inconsistencies"]:
            if "Status mismatch" in inconsistency or "status" in inconsistency.lower():
                # Extract worker name and fix status
                parts = inconsistency.split(":")
                if len(parts) > 0:
                    worker_name = parts[0].strip()
                    logger.info(f"Fixing status for: {worker_name}")
                    if not dry_run:
                        self._fix_worker_status(worker_name)
                        fixes["status_updated"] += 1
        
        # 6. Sync permanent workers to Redis
        logger.info(f"\nSyncing permanent workers to Redis: {self.permanent_workers}")
        if not dry_run:
            self._sync_permanent_workers()
        
        logger.info("\n" + "=" * 80)
        logger.info("FIXES APPLIED:")
        logger.info(f"  - Redis entries cleaned: {fixes['redis_cleaned']}")
        logger.info(f"  - MongoDB entries cleaned: {fixes['mongo_cleaned']}")
        logger.info(f"  - Kubernetes nodes removed: {fixes['k8s_cleaned']}")
        logger.info(f"  - Worker counter fixed: {fixes['counter_fixed']}")
        logger.info(f"  - Status updates: {fixes['status_updated']}")
        logger.info("=" * 80)
        
        return fixes
    
    def _get_docker_workers(self) -> List[Dict]:
        """Get all Docker workers"""
        workers = []
        containers = self.docker_client.containers.list(all=True)
        
        for container in containers:
            if container.name and container.name.startswith(self.worker_prefix):
                workers.append({
                    "name": container.name,
                    "id": container.id[:12],
                    "status": container.status
                })
        
        return sorted(workers, key=lambda w: w['name'])
    
    def _get_k8s_workers(self) -> List[Dict]:
        """Get all Kubernetes workers"""
        if not self.k8s_api:
            return []
        
        workers = []
        try:
            nodes = self.k8s_api.list_node()
            
            for node in nodes.items:
                # Skip control plane
                if node.metadata.labels and \
                   'node-role.kubernetes.io/control-plane' in node.metadata.labels:
                    continue
                
                is_ready = False
                if node.status.conditions:
                    for condition in node.status.conditions:
                        if condition.type == "Ready" and condition.status == "True":
                            is_ready = True
                            break
                
                workers.append({
                    "name": node.metadata.name,
                    "ready": is_ready
                })
        except Exception as e:
            logger.error(f"Error getting Kubernetes workers: {e}")
        
        return sorted(workers, key=lambda w: w['name'])
    
    def _get_redis_workers(self) -> List[Dict]:
        """Get all Redis workers"""
        workers = []

        # Scan for worker keys (correct pattern: autoscaler:worker:* not autoscaler:workers:*)
        for key in self.redis_client.scan_iter("autoscaler:worker:*"):
            # Extract worker name
            parts = key.split(":")
            if len(parts) >= 3:
                worker_name = parts[2]

                # Get status
                status = self.redis_client.hget(key, "status")

                workers.append({
                    "name": worker_name,
                    "status": status or "unknown"
                })

        return sorted(workers, key=lambda w: w['name'])
    
    def _get_mongo_workers(self) -> List[Dict]:
        """Get all MongoDB workers"""
        workers = []
        
        try:
            for doc in self.db.workers.find():
                workers.append({
                    "name": doc.get("node_name"),
                    "status": doc.get("status")
                })
        except Exception as e:
            logger.error(f"Error getting MongoDB workers: {e}")
        
        return sorted(workers, key=lambda w: w['name'])
    
    def _get_max_worker_number(self, worker_names: Set[str]) -> int:
        """Get the highest worker number"""
        max_num = 0
        
        for name in worker_names:
            try:
                # Extract number from name (e.g., k3s-worker-3 -> 3)
                num = int(name.split('-')[-1])
                max_num = max(max_num, num)
            except (ValueError, IndexError):
                pass
        
        return max_num
    
    def _get_redis_worker_counter(self) -> int:
        """Get current worker counter from Redis"""
        counter = self.redis_client.get("autoscaler:workers:next_number")
        return int(counter) if counter else 0
    
    def _clean_redis_worker(self, worker_name: str):
        """Clean Redis entries for a worker"""
        # Remove main worker key
        key = f"autoscaler:worker:{worker_name}"
        self.redis_client.delete(key)

        # Remove from any sets
        self.redis_client.srem("autoscaler:workers:all", worker_name)

        logger.info(f"✓ Cleaned Redis entries for {worker_name}")
    
    def _clean_mongo_worker(self, worker_name: str):
        """Clean MongoDB entries for a worker"""
        result = self.db.workers.delete_one({"node_name": worker_name})
        logger.info(f"✓ Cleaned MongoDB entry for {worker_name} (deleted: {result.deleted_count})")
    
    def _remove_k8s_node(self, node_name: str):
        """Remove orphaned Kubernetes node"""
        if not self.k8s_api:
            return
        
        try:
            self.k8s_api.delete_node(
                name=node_name,
                body=client.V1DeleteOptions(
                    grace_period_seconds=0,
                    propagation_policy='Background'
                )
            )
            logger.info(f"✓ Removed Kubernetes node: {node_name}")
        except Exception as e:
            logger.error(f"Failed to remove Kubernetes node {node_name}: {e}")
    
    def _fix_worker_counter(self, correct_value: int):
        """Fix the worker counter in Redis"""
        self.redis_client.set("autoscaler:workers:next_number", correct_value)
        logger.info(f"✓ Set worker counter to {correct_value}")
    
    def _fix_worker_status(self, worker_name: str):
        """Fix worker status in Redis"""
        # Check if container is running
        try:
            container = self.docker_client.containers.get(worker_name)
            if container.status == 'running':
                # Update to ready
                key = f"autoscaler:worker:{worker_name}"
                self.redis_client.hset(key, "status", "ready")
                logger.info(f"✓ Updated status to 'ready' for {worker_name}")
        except docker.errors.NotFound:
            logger.warning(f"Container {worker_name} not found in Docker")
    
    def _sync_permanent_workers(self):
        """Sync permanent workers to Redis"""
        self.redis_client.set(
            "autoscaler:workers:permanent",
            json.dumps(self.permanent_workers)
        )
        logger.info(f"✓ Synced permanent workers to Redis")


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Fix K3s Autoscaler data integrity issues")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fixed without making changes")
    parser.add_argument("--diagnose-only", action="store_true", help="Only diagnose issues, don't fix")
    parser.add_argument("--redis-host", default="localhost", help="Redis host")
    parser.add_argument("--redis-port", type=int, default=6379, help="Redis port")
    parser.add_argument("--mongo-url", default="mongodb://admin:password@localhost:27017/autoscaler?authSource=admin")
    parser.add_argument("--kubeconfig", default="./kubeconfig/kubeconfig")
    
    args = parser.parse_args()
    
    fixer = DataIntegrityFixer(
        redis_host=args.redis_host,
        redis_port=args.redis_port,
        mongo_url=args.mongo_url,
        kubeconfig_path=args.kubeconfig
    )
    
    if args.diagnose_only:
        issues = fixer.diagnose()
        print("\n" + "=" * 80)
        print("SUMMARY:")
        print(f"  Docker workers: {len(issues['docker_workers'])}")
        print(f"  Kubernetes workers: {len(issues['k8s_workers'])}")
        print(f"  Redis workers: {len(issues['redis_workers'])}")
        print(f"  MongoDB workers: {len(issues['mongo_workers'])}")
        print(f"  Inconsistencies: {len(issues['inconsistencies'])}")
        print(f"  Orphaned K8s nodes: {len(issues['orphaned_nodes'])}")
        print(f"  Stale Redis keys: {len(issues['stale_redis_keys'])}")
        print(f"  Wrong worker counter: {issues['wrong_worker_counter']}")
        print("=" * 80)
    else:
        fixes = fixer.fix_all(dry_run=args.dry_run)
        
        if args.dry_run:
            print("\n✓ Dry run complete. Run without --dry-run to apply fixes.")
        else:
            print("\n✓ All fixes applied successfully!")


if __name__ == "__main__":
    main()
