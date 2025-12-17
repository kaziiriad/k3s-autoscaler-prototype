#!/usr/bin/env python3
"""
Improved Event Bus Implementation
Based on the fixes in event_bus_fixes.py
"""

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ImprovedEventBusIntegration:
    """Better way to integrate event bus with autoscaler"""

    def __init__(self, autoscaler):
        self.autoscaler = autoscaler
        self.event_bus = None
        self.event_bus_task = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize event bus with proper error handling"""
        async with self._lock:
            if self._initialized:
                return True

            try:
                from .event_bus import EventBus

                # Create event bus
                self.event_bus = EventBus()

                # Connect with timeout
                try:
                    await asyncio.wait_for(
                        self.event_bus.connect(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    logger.error("Event bus connection timeout")
                    return False

                # Start event bus consumer loop
                self.event_bus_task = asyncio.create_task(
                    self._run_event_bus_with_recovery()
                )

                # Register handlers
                await self._register_handlers()

                self._initialized = True
                logger.info("✓ Event bus initialized successfully")
                return True

            except Exception as e:
                logger.error(f"Failed to initialize event bus: {e}")
                self._initialized = False
                return False

    async def _run_event_bus_with_recovery(self):
        """Run event bus with automatic recovery"""
        retry_count = 0
        max_retries = 5

        while retry_count < max_retries:
            try:
                await self.event_bus.start()
                # If we get here, event bus stopped normally
                break
            except Exception as e:
                retry_count += 1
                logger.error(
                    f"Event bus crashed (attempt {retry_count}/{max_retries}): {e}"
                )

                if retry_count < max_retries:
                    # Exponential backoff
                    wait_time = min(2 ** retry_count, 60)
                    logger.info(f"Reconnecting in {wait_time}s...")
                    await asyncio.sleep(wait_time)

                    # Try to reconnect
                    try:
                        await self.event_bus.connect()
                    except Exception as reconnect_error:
                        logger.error(f"Reconnection failed: {reconnect_error}")

        if retry_count >= max_retries:
            logger.critical("Event bus failed permanently after max retries")

    async def _register_handlers(self):
        """Register event handlers"""
        from . import EventType
        from .handlers import (
            OptimalStateHandler,
            ScalingCompletedHandler,
            ClusterStateHandler,
            MinimumNodeEnforcementHandler
        )

        # Create handler instances
        handlers = [
            OptimalStateHandler(),
            ScalingCompletedHandler(),
            ClusterStateHandler(),
            MinimumNodeEnforcementHandler()
        ]

        # Subscribe handlers to their events
        for handler in handlers:
            for event_type in handler.subscribed_events:
                await self.event_bus.subscribe(event_type, handler)

    async def publish(self, event):
        """Publish event with fallback if event bus not initialized"""
        if not self._initialized or not self.event_bus:
            logger.warning(
                f"Event bus not initialized, logging event instead: {event.event_type}"
            )
            # Fallback: just log it
            logger.info(f"EVENT: {event.event_type.value} - {event.data}")
            return False

        try:
            return await asyncio.wait_for(
                self.event_bus.publish(event),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Event publish timeout: {event.event_type}")
            return False
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            return False

    async def shutdown(self):
        """Graceful shutdown"""
        if self.event_bus_task:
            self.event_bus_task.cancel()
            try:
                await self.event_bus_task
            except asyncio.CancelledError:
                pass

        if self.event_bus:
            await self.event_bus.disconnect()


class SafeEventEmitter:
    """
    Safe way to emit events from sync code
    Handles all the edge cases properly
    """

    def __init__(self, event_bus_integration: ImprovedEventBusIntegration):
        self.event_bus = event_bus_integration
        self._emit_queue = asyncio.Queue()
        self._background_task = None

    async def start_background_publisher(self):
        """Start background task to publish queued events"""
        self._background_task = asyncio.create_task(self._publish_loop())

    async def _publish_loop(self):
        """Background loop to publish queued events"""
        while True:
            try:
                event = await self._emit_queue.get()
                if event is None:  # Shutdown signal
                    break

                await self.event_bus.publish(event)
            except Exception as e:
                logger.error(f"Background publisher error: {e}")

    def emit_sync(self, event):
        """
        Safely emit event from synchronous code
        This is the method to use in autoscaler.py
        """
        try:
            # Try to get running loop
            loop = asyncio.get_running_loop()

            # Queue the event for background publishing
            loop.call_soon_threadsafe(self._emit_queue.put_nowait, event)

            logger.debug(f"Queued event for publishing: {event.event_type.value}")

        except RuntimeError:
            # No running loop - we're in truly sync context
            # Just log it
            logger.warning(
                f"No event loop, logging event: {event.event_type.value}"
            )
            logger.info(f"EVENT: {event.event_type.value} - {event.data}")

    async def shutdown(self):
        """Shutdown background publisher"""
        if self._background_task:
            await self._emit_queue.put(None)  # Shutdown signal
            await self._background_task


class EventBusHealthMonitor:
    """Monitor event bus health and alert on issues"""

    def __init__(self, event_bus_integration: ImprovedEventBusIntegration):
        self.event_bus = event_bus_integration
        self.health_check_interval = 30  # seconds
        self.last_publish_time = None
        self.consecutive_failures = 0
        self.max_failures = 5

    async def start(self):
        """Start health monitoring"""
        while True:
            try:
                await asyncio.sleep(self.health_check_interval)
                await self._check_health()
            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def _check_health(self):
        """Check event bus health"""
        if not self.event_bus._initialized:
            logger.warning("Event bus not initialized")
            return

        try:
            # Try to publish a heartbeat event
            from . import Event, EventType

            heartbeat = Event(
                event_type=EventType.AUTOSCALER_STARTED,  # Use as heartbeat
                source="health_monitor",
                data={"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()}
            )

            success = await asyncio.wait_for(
                self.event_bus.publish(heartbeat),
                timeout=5.0
            )

            if success:
                self.consecutive_failures = 0
                self.last_publish_time = datetime.now(timezone.utc)
            else:
                self._handle_failure()

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self._handle_failure()

    def _handle_failure(self):
        """Handle health check failure"""
        self.consecutive_failures += 1

        if self.consecutive_failures >= self.max_failures:
            logger.critical(
                f"Event bus unhealthy: {self.consecutive_failures} consecutive failures"
            )
            # TODO: Trigger alert/notification
        else:
            logger.warning(
                f"Event bus failure {self.consecutive_failures}/{self.max_failures}"
            )