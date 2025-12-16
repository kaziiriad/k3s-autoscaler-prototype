#!/usr/bin/env python3
"""
Minimum Node Enforcer service for the autoscaler
Monitors worker node count and ensures minimum nodes are maintained
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from events import (
    EventBus,
    Event,
    EventType,
    MinimumNodesViolation,
    AutoScaleUpToMinimum,
    MinimumNodesRestored,
    EmergencyScaleUp
)
from core.autoscaler import K3sAutoscaler
from config.settings import settings
from database.redis_client import AutoscalerRedisClient

logger = logging.getLogger(__name__)


class MinimumNodeEnforcer:
    """Service to enforce minimum worker nodes in the cluster"""

    def __init__(self, autoscaler: K3sAutoscaler, event_bus: EventBus):
        """
        Initialize the Minimum Node Enforcer

        Args:
            autoscaler: The main autoscaler instance
            event_bus: The event bus for publishing events
        """
        self.autoscaler = autoscaler
        self.event_bus = event_bus
        self.running = False
        self.min_workers = settings.autoscaler.min_nodes

        # Initialize Redis client for persistent storage
        self.redis = AutoscalerRedisClient(
            host=settings.redis.host,
            port=settings.redis.port,
            db=settings.redis.cache_db,
            password=settings.redis.password
        )

    async def start(self):
        """Start the minimum node enforcer"""
        if self.running:
            logger.warning("Minimum Node Enforcer is already running")
            return

        self.running = True
        logger.info(f"Starting Minimum Node Enforcer (min_nodes={self.min_workers})")

        # Subscribe to relevant events
        await self.event_bus.subscribe(EventType.CLUSTER_STATE_SYNCED, self)
        await self.event_bus.subscribe(EventType.NODE_HEALTH_DEGRADED, self)
        await self.event_bus.subscribe(EventType.NODE_UNEXPECTEDLY_LOST, self)
        await self.event_bus.subscribe(EventType.SCALING_ACTION_COMPLETED, self)

        # Start monitoring loop
        asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop the minimum node enforcer"""
        self.running = False
        logger.info("Stopping Minimum Node Enforcer")

    async def handle(self, event: Event) -> bool:
        """
        Handle events that might affect minimum node requirements

        Args:
            event: The event to handle

        Returns:
            True if handled successfully
        """
        try:
            if event.event_type == EventType.CLUSTER_STATE_SYNCED:
                return await self._handle_cluster_state_synced(event)
            elif event.event_type == EventType.NODE_HEALTH_DEGRADED:
                return await self._handle_node_health_degraded(event)
            elif event.event_type == EventType.NODE_UNEXPECTEDLY_LOST:
                return await self._handle_node_lost(event)
            elif event.event_type == EventType.SCALING_ACTION_COMPLETED:
                return await self._handle_scaling_completed(event)

            return True

        except Exception as e:
            logger.error(f"MinimumNodeEnforcer failed to handle event {event.event_type}: {e}")
            return False

    async def _monitor_loop(self):
        """Monitor worker count and enforce minimum nodes"""
        while self.running:
            try:
                await self._check_minimum_nodes()
                await asyncio.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                await asyncio.sleep(5)  # Brief pause before retrying

    async def _check_minimum_nodes(self):
        """Check if minimum nodes requirement is satisfied"""
        # Update last check time in Redis
        self.redis.set("min_enforcer:last_check", datetime.now(timezone.utc).isoformat())

        # Get current worker count from autoscaler
        try:
            current_workers = self.autoscaler.database.get_worker_count()

            if current_workers < self.min_workers:
                await self._handle_violation(current_workers)
            elif current_workers == self.min_workers:
                await self._handle_minimum_satisfied(current_workers)

        except Exception as e:
            logger.error(f"Failed to check minimum nodes: {e}")

    async def _handle_violation(self, current_workers: int):
        """Handle minimum nodes violation"""
        shortage = self.min_workers - current_workers

        # Increment violations counter in Redis
        violations_count = int(self.redis.increment("min_enforcer:violations_count"))

        # Get last check time from Redis
        last_check_str = self.redis.get("min_enforcer:last_check")

        logger.warning(
            f"Minimum nodes violation: {current_workers}/{self.min_workers} workers "
            f"(shortage: {shortage})"
        )

        # Publish violation event
        violation_event = MinimumNodesViolation(
            source="MinimumNodeEnforcer",
            data={
                "current_workers": current_workers,
                "min_workers": self.min_workers,
                "shortage": shortage,
                "violations_count": violations_count,
                "last_check": last_check_str.decode() if last_check_str else None
            }
        )
        await self.event_bus.publish(violation_event)

        # Check if we need emergency scale-up (lost more than 50% of min nodes)
        if current_workers < self.min_workers // 2:
            await self._emergency_scale_up(current_workers)
        else:
            await self._scale_to_minimum(current_workers)

    async def _handle_minimum_satisfied(self, current_workers: int):
        """Handle when minimum nodes are satisfied"""
        # Get violations count from Redis
        violations_count = int(self.redis.get("min_enforcer:violations_count") or 0)
        enforcements_count = int(self.redis.get("min_enforcer:enforcements_count") or 0)

        # Only publish restoration event if we had a violation before
        if violations_count > 0:
            logger.info(
                f"Minimum nodes restored: {current_workers}/{self.min_workers} workers"
            )

            restoration_event = MinimumNodesRestored(
                source="MinimumNodeEnforcer",
                data={
                    "current_workers": current_workers,
                    "min_workers": self.min_workers,
                    "violations_count": violations_count,
                    "enforcements_count": enforcements_count
                }
            )
            await self.event_bus.publish(restoration_event)

            # Reset violations count after restoration
            self.redis.set("min_enforcer:violations_count", 0)

    async def _scale_to_minimum(self, current_workers: int):
        """Scale up to minimum workers"""
        scale_up_count = self.min_workers - current_workers

        # Check cooldown before scaling
        if self.autoscaler.database.is_cooldown_active("scale_up"):
            remaining = self.autoscaler.database.get_cooldown_remaining("scale_up")
            logger.info(f"Scale-up cooldown active: {remaining}s remaining")
            return

        logger.info(
            f"Auto-scaling to minimum: adding {scale_up_count} workers "
            f"({current_workers} → {self.min_workers})"
        )

        # Publish auto-scale event
        auto_scale_event = AutoScaleUpToMinimum(
            source="MinimumNodeEnforcer",
            data={
                "current_workers": current_workers,
                "min_workers": self.min_workers,
                "scale_up_count": scale_up_count,
                "reason": "Minimum nodes enforcement"
            }
        )
        await self.event_bus.publish(auto_scale_event)

        # Execute scaling
        try:
            # Use a special reason for minimum node enforcement
            success = await self.autoscaler.scale_up(
                count=scale_up_count,
                reason="Auto-scaling to minimum nodes"
            )

            if success:
                # Increment enforcements counter in Redis
                self.redis.increment("min_enforcer:enforcements_count")
                logger.info(f"Successfully initiated scale-up for minimum nodes")
            else:
                logger.error(f"Failed to initiate scale-up for minimum nodes")

        except Exception as e:
            logger.error(f"Error scaling to minimum nodes: {e}")

    async def _emergency_scale_up(self, current_workers: int):
        """Emergency scale-up when too many nodes are lost"""
        lost_count = self.min_workers - current_workers
        # Scale up more than minimum to provide buffer
        emergency_scale = min(lost_count + 1, self.min_workers)

        logger.error(
            f"Emergency scale-up triggered: lost {lost_count} workers, "
            f"scaling up by {emergency_scale}"
        )

        # Publish emergency scale-up event
        emergency_event = EmergencyScaleUp(
            source="MinimumNodeEnforcer",
            data={
                "lost_count": lost_count,
                "remaining_workers": current_workers,
                "emergency_scale": emergency_scale,
                "min_workers": self.min_workers,
                "reason": "Emergency: Too many workers lost"
            }
        )
        await self.event_bus.publish(emergency_event)

        # Execute emergency scaling (bypass cooldown)
        try:
            success = await self.autoscaler.scale_up(
                count=emergency_scale,
                reason="Emergency scale-up - too many workers lost"
            )

            if success:
                self.enforcements_count += 1
                logger.info(f"Successfully initiated emergency scale-up")
            else:
                logger.error(f"Failed to initiate emergency scale-up")

        except Exception as e:
            logger.error(f"Error in emergency scale-up: {e}")

    async def _handle_cluster_state_synced(self, event: Event) -> bool:
        """Handle cluster state synced event"""
        # Get worker count from event data
        db_workers = event.data.get("db_workers", 0)

        if db_workers < self.min_workers:
            await self._handle_violation(db_workers)
        elif db_workers == self.min_workers:
            await self._handle_minimum_satisfied(db_workers)

        return True

    async def _handle_node_health_degraded(self, event: Event) -> bool:
        """Handle node health degraded event"""
        node_name = event.data.get("node_name")
        status = event.data.get("status")

        logger.warning(f"Node health degraded: {node_name} status={status}")

        # Trigger an immediate check
        await asyncio.sleep(5)  # Give a moment for potential recovery
        await self._check_minimum_nodes()

        return True

    async def _handle_node_lost(self, event: Event) -> bool:
        """Handle node unexpectedly lost event"""
        node_name = event.data.get("node_name")

        logger.error(f"Node unexpectedly lost: {node_name}")

        # Immediate check and potential emergency response
        await self._check_minimum_nodes()

        return True

    async def _handle_scaling_completed(self, event: Event) -> bool:
        """Handle scaling action completed event"""
        action = event.data.get("action")
        new_workers = event.data.get("new_workers", 0)

        if action == "scale_up" and new_workers >= self.min_workers:
            # Minimum restored through scaling
            await self._handle_minimum_satisfied(new_workers)

        return True

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the minimum node enforcer"""
        return {
            "running": self.running,
            "min_workers": self.min_workers,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "violations_count": self.violations_count,
            "enforcements_count": self.enforcements_count,
            "current_workers": len(self.autoscaler.worker_nodes) if self.autoscaler else 0
        }