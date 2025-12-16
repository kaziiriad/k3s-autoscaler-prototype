#!/usr/bin/env python3
"""
Event handlers for the autoscaler event-driven architecture
"""

import logging
import json
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from .base import EventHandler, Event, EventType
from .core_events import (
    OptimalStateTrigger,
    ScalingActionCompleted,
    ClusterStateSynced,
    UnderutilizationWarning,
    ResourcePressureAlert,
    PendingPodsDetected,
    NodeHealthDegraded,
    MinimumNodesViolation,
    AutoScaleUpToMinimum,
    MinimumNodesRestored,
    EmergencyScaleUp,
    ScaleDownBlocked
)
from config.settings import settings
from database.redis_client import AutoscalerRedisClient

logger = logging.getLogger(__name__)


class OptimalStateHandler(EventHandler):
    """Handler for OptimalStateTrigger events"""

    def __init__(self):
        super().__init__("OptimalStateHandler")
        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.events_db,
            password=settings.redis.password
        )

    async def handle(self, event: OptimalStateTrigger) -> bool:
        """
        Handle optimal state trigger events

        Args:
            event: The OptimalStateTrigger event

        Returns:
            True if handled successfully
        """
        try:
            current_workers = event.data.get("current_workers", 0)
            min_workers = event.data.get("min_workers", 0)

            logger.info(
                f"🎯 Optimal state achieved: {current_workers} workers "
                f"(minimum: {min_workers}) at {event.timestamp}"
            )

            # Store in Redis instead of memory
            optimal_key = "handler:optimal_state:last_time"
            periods_key = "handler:optimal_state:periods"

            # Update last optimal time
            self.redis.set(optimal_key, event.timestamp.isoformat())

            # Add to periods list (Redis list)
            period_data = {
                "start_time": event.timestamp.isoformat(),
                "workers": current_workers,
                "min_workers": min_workers
            }

            # Use Redis list for periods (LPUSH, then trim to keep only 10)
            self.redis.lpush(periods_key, json.dumps(period_data))
            self.redis.ltrim(periods_key, 0, 9)  # Keep only 10 most recent periods

            # Check if we've been at optimal state for a while
            all_periods = self.redis.lrange(periods_key, 0, -1)
            if len(all_periods) >= 2:
                # Get second last period
                second_last = json.loads(all_periods[-1])
                duration_seconds = (
                    event.timestamp -
                    datetime.fromisoformat(second_last["start_time"])
                ).total_seconds()

                if duration_seconds > 300:  # 5 minutes
                    logger.info(
                        f"✅ Cluster has maintained optimal state for {duration_seconds:.0f} seconds"
                    )

            return True

        except Exception as e:
            logger.error(f"OptimalStateHandler failed: {e}")
            return False


class ScalingCompletedHandler(EventHandler):
    """Handler for ScalingActionCompleted events"""

    def __init__(self):
        super().__init__("ScalingCompletedHandler")
        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.events_db,
            password=settings.redis.password
        )

    async def handle(self, event: ScalingActionCompleted) -> bool:
        """
        Handle scaling action completed events

        Args:
            event: The ScalingActionCompleted event

        Returns:
            True if handled successfully
        """
        try:
            action = event.data.get("action", "unknown")
            previous_workers = event.data.get("previous_workers", 0)
            new_workers = event.data.get("new_workers", 0)

            logger.info(
                f"📊 Scaling completed: {action} from {previous_workers} to {new_workers} workers"
            )

            # Store in Redis instead of memory
            history_key = "handler:scaling:history"
            last_time_key = "handler:scaling:last_time"
            up_count_key = "handler:scaling:count:up"
            down_count_key = "handler:scaling:count:down"

            # Update last scale time
            self.redis.set(last_time_key, event.timestamp.isoformat())

            # Update counters based on action
            if action == "scale_up":
                self.redis.incr(up_count_key)
            elif action == "scale_down":
                self.redis.incr(down_count_key)

            # Add to history list (Redis list)
            scaling_record = {
                "timestamp": event.timestamp.isoformat(),
                "action": action,
                "previous_workers": previous_workers,
                "new_workers": new_workers,
                "worker_change": new_workers - previous_workers
            }

            # Use Redis list for history (LPUSH, then trim to keep only 100)
            self.redis.lpush(history_key, json.dumps(scaling_record))
            self.redis.ltrim(history_key, 0, 99)  # Keep only 100 most recent records

            # Check for recent scaling activity (last hour)
            all_records = self.redis.lrange(history_key, 0, -1)
            recent_scales = []
            current_time = event.timestamp

            for record_json in all_records:
                record = json.loads(record_json)
                record_time = datetime.fromisoformat(record["timestamp"])
                if (current_time - record_time).total_seconds() < 3600:
                    recent_scales.append(record)

            if len(recent_scales) > 5:
                logger.warning(
                    f"⚠️  High scaling activity detected: {len(recent_scales)} scaling actions in the last hour"
                )

            return True

        except Exception as e:
            logger.error(f"ScalingCompletedHandler failed: {e}")
            return False

    def get_scaling_stats(self) -> Dict[str, Any]:
        """Get scaling statistics from Redis"""
        try:
            # Get counters from Redis
            up_count = int(self.redis.get("handler:scaling:count:up") or 0)
            down_count = int(self.redis.get("handler:scaling:count:down") or 0)
            last_time_str = self.redis.get("handler:scaling:last_time")

            # Get recent scaling count (last hour)
            all_records = self.redis.lrange("handler:scaling:history", 0, -1)
            recent_count = 0

            if last_time_str:
                last_time = datetime.fromisoformat(last_time_str.decode())
                current_time = datetime.now(timezone.utc)

                # Count records in the last hour
                for record_json in all_records:
                    record = json.loads(record_json)
                    record_time = datetime.fromisoformat(record["timestamp"])
                    if (current_time - record_time).total_seconds() < 3600:
                        recent_count += 1

            return {
                "total_scale_ups": up_count,
                "total_scale_downs": down_count,
                "last_scale_time": last_time_str.decode() if last_time_str else None,
                "recent_scales": recent_count
            }
        except Exception as e:
            logger.error(f"Failed to get scaling stats: {e}")
            return {
                "total_scale_ups": 0,
                "total_scale_downs": 0,
                "last_scale_time": None,
                "recent_scales": 0
            }


class ClusterStateHandler(EventHandler):
    """Handler for ClusterStateSynced events"""

    def __init__(self):
        super().__init__("ClusterStateHandler")
        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.events_db,
            password=settings.redis.password
        )

    async def handle(self, event: ClusterStateSynced) -> bool:
        """
        Handle cluster state synced events

        Args:
            event: The ClusterStateSynced event

        Returns:
            True if handled successfully
        """
        try:
            k8s_nodes = event.data.get("k8s_nodes", 0)
            docker_containers = event.data.get("docker_containers", 0)
            db_workers = event.data.get("db_workers", 0)

            logger.info(
                f"🔄 Cluster state synced: K8s={k8s_nodes}, Docker={docker_containers}, DB={db_workers}"
            )

            # Check for inconsistencies
            inconsistencies = []
            if k8s_nodes != docker_containers:
                inconsistencies.append(f"K8s nodes ({k8s_nodes}) != Docker containers ({docker_containers})")
            if docker_containers != db_workers:
                inconsistencies.append(f"Docker containers ({docker_containers}) != DB workers ({db_workers})")

            if inconsistencies:
                logger.warning(f"⚠️  Cluster state inconsistencies detected:")
                for issue in inconsistencies:
                    logger.warning(f"   - {issue}")
            else:
                logger.debug("✅ Cluster state is consistent across all sources")

            # Store in Redis instead of memory
            last_time_key = "handler:sync:last_time"
            history_key = "handler:sync:history"

            # Update last sync time
            self.redis.set(last_time_key, event.timestamp.isoformat())

            # Add to history list (Redis list)
            sync_record = {
                "timestamp": event.timestamp.isoformat(),
                "k8s_nodes": k8s_nodes,
                "docker_containers": docker_containers,
                "db_workers": db_workers,
                "inconsistencies": len(inconsistencies)
            }

            # Use Redis list for history (LPUSH, then trim to keep only 50)
            self.redis.lpush(history_key, json.dumps(sync_record))
            self.redis.ltrim(history_key, 0, 49)  # Keep only 50 most recent records

            return True

        except Exception as e:
            logger.error(f"ClusterStateHandler failed: {e}")
            return False


class MinimumNodeEnforcementHandler(EventHandler):
    """Handler for minimum node enforcement events"""

    def __init__(self):
        super().__init__("MinimumNodeEnforcementHandler")
        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.events_db,
            password=settings.redis.password
        )

    async def handle(self, event: Event) -> bool:
        """
        Handle minimum node enforcement events

        Args:
            event: The minimum node enforcement event

        Returns:
            True if handled successfully
        """
        try:
            if isinstance(event, MinimumNodesViolation):
                return await self._handle_violation(event)
            elif isinstance(event, AutoScaleUpToMinimum):
                return await self._handle_auto_scale(event)
            elif isinstance(event, MinimumNodesRestored):
                return await self._handle_restored(event)
            elif isinstance(event, EmergencyScaleUp):
                return await self._handle_emergency(event)

            return True

        except Exception as e:
            logger.error(f"MinimumNodeEnforcementHandler failed: {e}")
            return False

    async def _handle_violation(self, event: MinimumNodesViolation) -> bool:
        """Handle minimum nodes violation"""
        current_workers = event.data.get("current_workers", 0)
        min_workers = event.data.get("min_workers", 0)

        logger.warning(
            f"🚨 Minimum nodes violation: {current_workers} workers "
            f"(minimum required: {min_workers})"
        )

        # Store in Redis instead of memory
        violations_key = "handler:min_enforcement:violations"

        violation_record = {
            "timestamp": event.timestamp.isoformat(),
            "current_workers": current_workers,
            "min_workers": min_workers,
            "shortage": min_workers - current_workers
        }

        # Use Redis list for violations (LPUSH, then trim to keep only 20)
        self.redis.lpush(violations_key, json.dumps(violation_record))
        self.redis.ltrim(violations_key, 0, 19)  # Keep only 20 most recent violations

        return True

    async def _handle_auto_scale(self, event: AutoScaleUpToMinimum) -> bool:
        """Handle auto scale-up to minimum"""
        current_workers = event.data.get("current_workers", 0)
        min_workers = event.data.get("min_workers", 0)
        scale_up_count = event.data.get("scale_up_count", 0)

        logger.info(
            f"🔧 Auto-scaling to minimum: adding {scale_up_count} workers "
            f"({current_workers} → {min_workers})"
        )

        # Store in Redis instead of memory
        enforcements_key = "handler:min_enforcement:enforcements"

        enforcement_record = {
            "timestamp": event.timestamp.isoformat(),
            "type": "auto_scale",
            "current_workers": current_workers,
            "target_workers": min_workers,
            "scale_up_count": scale_up_count
        }

        # Use Redis list for enforcements
        self.redis.lpush(enforcements_key, json.dumps(enforcement_record))

        return True

    async def _handle_restored(self, event: MinimumNodesRestored) -> bool:
        """Handle minimum nodes restored"""
        current_workers = event.data.get("current_workers", 0)
        min_workers = event.data.get("min_workers", 0)

        logger.info(
            f"✅ Minimum nodes restored: {current_workers} workers "
            f"(minimum: {min_workers})"
        )

        # Store in Redis instead of memory
        enforcements_key = "handler:min_enforcement:enforcements"

        enforcement_record = {
            "timestamp": event.timestamp.isoformat(),
            "type": "restored",
            "current_workers": current_workers,
            "min_workers": min_workers
        }

        # Use Redis list for enforcements
        self.redis.lpush(enforcements_key, json.dumps(enforcement_record))

        return True

    async def _handle_emergency(self, event: EmergencyScaleUp) -> bool:
        """Handle emergency scale-up"""
        lost_count = event.data.get("lost_count", 0)
        remaining_workers = event.data.get("remaining_workers", 0)
        emergency_scale = event.data.get("emergency_scale", 0)

        logger.error(
            f"🚨 Emergency scale-up: lost {lost_count} workers, "
            f"scaling up by {emergency_scale} ({remaining_workers} → {remaining_workers + emergency_scale})"
        )

        # Store in Redis instead of memory
        enforcements_key = "handler:min_enforcement:enforcements"

        enforcement_record = {
            "timestamp": event.timestamp.isoformat(),
            "type": "emergency",
            "lost_count": lost_count,
            "remaining_workers": remaining_workers,
            "emergency_scale": emergency_scale
        }

        # Use Redis list for enforcements
        self.redis.lpush(enforcements_key, json.dumps(enforcement_record))

        return True

    def get_enforcement_stats(self) -> Dict[str, Any]:
        """Get enforcement statistics from Redis"""
        try:
            # Get violations and enforcements from Redis
            violations = self.redis.lrange("handler:min_enforcement:violations", 0, -1)
            enforcements = self.redis.lrange("handler:min_enforcement:enforcements", 0, -1)

            # Count recent enforcements (last 24 hours)
            recent_enforcements = []
            current_time = datetime.now(timezone.utc)

            for enforcement_json in enforcements:
                enforcement = json.loads(enforcement_json)
                enforcement_time = datetime.fromisoformat(enforcement["timestamp"])
                if (current_time - enforcement_time).total_seconds() < 86400:
                    recent_enforcements.append(enforcement)

            # Get last enforcement
            last_enforcement = None
            if enforcements:
                last_enforcement = json.loads(enforcements[0])  # Most recent is at index 0

            return {
                "total_violations": len(violations),
                "total_enforcements": len(enforcements),
                "recent_enforcements_24h": len(recent_enforcements),
                "last_enforcement": last_enforcement
            }
        except Exception as e:
            logger.error(f"Failed to get enforcement stats: {e}")
            return {
                "total_violations": 0,
                "total_enforcements": 0,
                "recent_enforcements_24h": 0,
                "last_enforcement": None
            }


class ResourcePressureHandler(EventHandler):
    """Handler for resource pressure and utilization events"""

    def __init__(self):
        super().__init__("ResourcePressureHandler")
        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.events_db,
            password=settings.redis.password
        )

    async def handle(self, event: Event) -> bool:
        """
        Handle resource pressure events

        Args:
            event: The resource event

        Returns:
            True if handled successfully
        """
        try:
            if isinstance(event, ResourcePressureAlert):
                return await self._handle_pressure_alert(event)
            elif isinstance(event, UnderutilizationWarning):
                return await self._handle_underutilization(event)
            elif isinstance(event, PendingPodsDetected):
                return await self._handle_pending_pods(event)

            return True

        except Exception as e:
            logger.error(f"ResourcePressureHandler failed: {e}")
            return False

    async def _handle_pressure_alert(self, event: ResourcePressureAlert) -> bool:
        """Handle resource pressure alert"""
        cpu_avg = event.data.get("cpu_avg", 0)
        memory_avg = event.data.get("memory_avg", 0)
        workers = event.data.get("workers", 0)

        logger.warning(
            f"⚠️  Resource pressure: CPU={cpu_avg:.1f}%, Memory={memory_avg:.1f}% "
            f"across {workers} workers"
        )

        self.pressure_events.append({
            "timestamp": event.timestamp.isoformat(),
            "cpu_avg": cpu_avg,
            "memory_avg": memory_avg,
            "workers": workers
        })

        return True

    async def _handle_underutilization(self, event: UnderutilizationWarning) -> bool:
        """Handle underutilization warning"""
        workers = event.data.get("workers", 0)
        cpu_avg = event.data.get("cpu_avg", 0)
        memory_avg = event.data.get("memory_avg", 0)

        logger.info(
            f"💤 Underutilization detected: {workers} workers, "
            f"CPU={cpu_avg:.1f}%, Memory={memory_avg:.1f}%"
        )

        self.underutilization_events.append({
            "timestamp": event.timestamp.isoformat(),
            "workers": workers,
            "cpu_avg": cpu_avg,
            "memory_avg": memory_avg
        })

        return True

    async def _handle_pending_pods(self, event: PendingPodsDetected) -> bool:
        """Handle pending pods detection"""
        pending_count = event.data.get("pending_count", 0)

        logger.info(f"⏳ Pending pods detected: {pending_count} pods waiting")

        return True


# Registry of available handlers
AVAILABLE_HANDLERS = {
    EventType.OPTIMAL_STATE_TRIGGER: OptimalStateHandler,
    EventType.SCALING_ACTION_COMPLETED: ScalingCompletedHandler,
    EventType.CLUSTER_STATE_SYNCED: ClusterStateHandler,
    EventType.MINIMUM_NODES_VIOLATION: MinimumNodeEnforcementHandler,
    EventType.AUTO_SCALE_UP_TO_MINIMUM: MinimumNodeEnforcementHandler,
    EventType.MINIMUM_NODES_RESTORED: MinimumNodeEnforcementHandler,
    EventType.EMERGENCY_SCALE_UP: MinimumNodeEnforcementHandler,
    EventType.RESOURCE_PRESSURE_ALERT: ResourcePressureHandler,
    EventType.UNDERUTILIZATION_WARNING: ResourcePressureHandler,
    EventType.PENDING_PODS_DETECTED: ResourcePressureHandler,
    EventType.NODE_HEALTH_DEGRADED: ResourcePressureHandler,
    EventType.SCALE_DOWN_BLOCKED: MinimumNodeEnforcementHandler,
}