#!/usr/bin/env python3
"""
Improved Event Bus Implementation
Based on the fixes in event_bus_fixes.py
"""

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from .event_metrics import (
    metrics_collector,
    OPTIMAL_STATE_EVENTS,
    SCALING_EVENTS,
    CLUSTER_STATE_EVENTS,
    RESOURCE_PRESSURE_EVENTS
)

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
        event_type_str = event.event_type.value if hasattr(event.event_type, 'value') else str(event.event_type)

        if not self._initialized or not self.event_bus:
            logger.warning(
                f"Event bus not initialized, logging event instead: {event_type_str}"
            )
            # Record failed publish
            metrics_collector.record_event_published(event_type_str, False)
            metrics_collector.record_error('not_initialized', 'event_bus')
            # Fallback: just log it
            logger.info(f"EVENT: {event_type_str} - {event.data}")
            return False

        try:
            success = await asyncio.wait_for(
                self.event_bus.publish(event),
                timeout=5.0
            )
            # Record successful publish
            metrics_collector.record_event_published(event_type_str, success)

            # Record specific event type metrics
            if hasattr(event, 'data'):
                self._record_specific_event_metrics(event)

            return success
        except asyncio.TimeoutError:
            logger.error(f"Event publish timeout: {event_type_str}")
            metrics_collector.record_event_published(event_type_str, False)
            metrics_collector.record_error('timeout', 'event_bus')
            return False
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            metrics_collector.record_event_published(event_type_str, False)
            metrics_collector.record_error('publish_failed', 'event_bus')
            return False

    def _record_specific_event_metrics(self, event):
        """Record metrics for specific event types"""
        try:
            event_type = str(event.event_type.value if hasattr(event.event_type, 'value') else event.event_type)
            data = event.data or {}

            # Record optimal state events
            if 'optimal' in event_type.lower():
                reason = data.get('reason', 'unknown')
                metrics_collector.record_optimal_state_event(reason)

            # Record scaling events
            elif 'scaling' in event_type.lower() or 'scale_' in event_type.lower():
                action = data.get('action', 'unknown')
                status = data.get('status', 'completed')
                metrics_collector.record_scaling_event(action, status)

            # Record cluster state events
            elif 'cluster' in event_type.lower():
                sync_type = data.get('sync_type', 'full')
                status = data.get('status', 'success')
                metrics_collector.record_cluster_state_event(sync_type, status)

            # Record resource pressure events
            elif 'pressure' in event_type.lower() or 'resource' in event_type.lower():
                resource_type = data.get('resource_type', 'cpu')
                pressure_level = data.get('pressure_level', 'medium')
                metrics_collector.record_resource_pressure(resource_type, pressure_level)

        except Exception as e:
            logger.warning(f"Failed to record specific event metrics: {e}")

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
                # Update queue size metric
                metrics_collector.update_queue_size('emit_queue', self._emit_queue.qsize())

                event = await self._emit_queue.get()
                if event is None:  # Shutdown signal
                    break

                # Record queue size after getting event
                metrics_collector.update_queue_size('emit_queue', self._emit_queue.qsize())

                # Time the publish operation
                import time
                start_time = time.time()

                success = await self.event_bus.publish(event)

                # Record processing metrics
                duration = time.time() - start_time
                event_type = str(event.event_type.value if hasattr(event.event_type, 'value') else event.event_type)

                # Note: We don't have handler info here, so use 'background_publisher' as handler name
                metrics_collector.record_event_processed(event_type, 'background_publisher', success, duration)

            except Exception as e:
                logger.error(f"Background publisher error: {e}")
                metrics_collector.record_error('background_publish_error', 'event_emitter')

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
            metrics_collector.update_health('event_bus', False)
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
                metrics_collector.update_health('event_bus', True)
                # Also update emitter health
                metrics_collector.update_health('event_emitter', True)
            else:
                self._handle_failure()

        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self._handle_failure()

    def _handle_failure(self):
        """Handle health check failure"""
        self.consecutive_failures += 1

        # Update health metrics
        metrics_collector.update_health('event_bus', False)
        metrics_collector.update_health('event_emitter', False)
        metrics_collector.record_error('health_check_failed', 'health_monitor')

        if self.consecutive_failures >= self.max_failures:
            logger.critical(
                f"Event bus unhealthy: {self.consecutive_failures} consecutive failures"
            )
            # TODO: Trigger alert/notification
        else:
            logger.warning(
                f"Event bus failure {self.consecutive_failures}/{self.max_failures}"
            )