#!/usr/bin/env/python3
"""
EventBus implementation using Redis Streams for event-driven architecture
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from collections import defaultdict
import redis.asyncio as aioredis
from .base import Event, EventHandler, EventType
from config.settings import settings

logger = logging.getLogger(__name__)


class EventBus:
    """Redis-based event bus for publishing and subscribing to events"""

    def __init__(self, stream_name: str = "autoscaler_events"):
        """
        Initialize the EventBus

        Args:
            stream_name: Name of the Redis stream for events
        """
        self.stream_name = stream_name
        self.redis_client: Optional[aioredis.Redis] = None
        self.subscribers: Dict[EventType, List[EventHandler]] = defaultdict(list)
        self.running = False
        self.consumer_group = f"{stream_name}_group"
        self.consumer_name = f"{stream_name}_consumer"

    async def connect(self):
        """Connect to Redis"""
        try:
            # Build Redis URL using settings
            redis_url = f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.events_db}"
            if settings.redis.password:
                redis_url = f"redis://:{settings.redis.password}@{settings.redis.host}:{settings.redis.port}/{settings.redis.events_db}"

            self.redis_client = aioredis.from_url(redis_url, decode_responses=True)
            await self.redis_client.ping()
            logger.info(f"Connected to EventBus at {redis_url} (using DB {settings.redis.events_db})")

            # Create consumer group if it doesn't exist
            try:
                await self.redis_client.xgroup_create(
                    self.stream_name,
                    self.consumer_group,
                    id='0',
                    mkstream=True
                )
                logger.info(f"Created consumer group {self.consumer_group}")
            except Exception as e:
                # Group might already exist
                logger.debug(f"Consumer group creation skipped: {e}")

        except Exception as e:
            logger.error(f"Failed to connect to EventBus: {e}")
            raise

    async def disconnect(self):
        """Disconnect from Redis"""
        self.running = False
        if self.redis_client:
            await self.redis_client.close()
            self.redis_client = None
            logger.info("Disconnected from EventBus")

    async def publish(self, event: Event) -> bool:
        """
        Publish an event to the event stream

        Args:
            event: The event to publish

        Returns:
            True if published successfully, False otherwise
        """
        if not self.redis_client:
            logger.error("EventBus not connected")
            return False

        # Try to publish with retry on connection errors
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                # Test connection before publishing
                await self.redis_client.ping()

                event_data = event.to_dict()
                # Flatten nested dictionaries for Redis streams
                flattened_data = {}
                for key, value in event_data.items():
                    if isinstance(value, dict):
                        for subkey, subvalue in value.items():
                            flattened_data[f"{key}.{subkey}"] = str(subvalue)
                    else:
                        flattened_data[key] = str(value)

                await self.redis_client.xadd(
                    self.stream_name,
                    flattened_data,
                    maxlen=10000  # Keep last 10000 events
                )
                logger.debug(f"Published event {event.event_type.value} ({event.event_id})")
                return True
            except (aioredis.ConnectionError, aioredis.RedisError) as e:
                if attempt < max_retries:
                    logger.warning(f"EventBus connection issue (attempt {attempt + 1}/{max_retries + 1}), reconnecting...")
                    try:
                        # Reconnect
                        if self.redis_client:
                            await self.redis_client.close()
                        await self.connect()
                    except Exception as reconnect_error:
                        logger.error(f"Failed to reconnect EventBus: {reconnect_error}")
                else:
                    logger.error(f"Failed to publish event {event.event_type.value} after {max_retries + 1} attempts: {e}")
                    return False
            except Exception as e:
                logger.error(f"Failed to publish event {event.event_type.value}: {e}")
                return False

    async def subscribe(self, event_type: EventType, handler: EventHandler):
        """
        Subscribe to an event type with a handler

        Args:
            event_type: The event type to subscribe to
            handler: The handler to process the event
        """
        handler.subscribe(event_type)
        self.subscribers[event_type].append(handler)
        logger.info(f"Subscribed handler {handler.name} to event {event_type.value}")

    async def unsubscribe(self, event_type: EventType, handler: EventHandler):
        """
        Unsubscribe a handler from an event type

        Args:
            event_type: The event type to unsubscribe from
            handler: The handler to remove
        """
        if handler in self.subscribers[event_type]:
            self.subscribers[event_type].remove(handler)
            handler.unsubscribe(event_type)
            logger.info(f"Unsubscribed handler {handler.name} from event {event_type.value}")

    async def _process_event(self, event_data: Dict[str, Any]) -> bool:
        """
        Process a single event by notifying all relevant handlers

        Args:
            event_data: Event data from Redis stream

        Returns:
            True if processed successfully, False otherwise
        """
        try:
            # Create event object
            event = Event.from_dict(event_data)
            event_type = event.event_type

            # Notify all handlers subscribed to this event type
            handlers = self.subscribers.get(event_type, [])
            if not handlers:
                return True

            # Process handlers concurrently
            tasks = []
            for handler in handlers:
                task = asyncio.create_task(handler.handle(event))
                tasks.append(task)

            # Wait for all handlers to complete
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                # Check for any failures
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        handler_name = handlers[i].name
                        logger.error(f"Handler {handler_name} failed for event {event_type.value}: {result}")

            return True
        except Exception as e:
            logger.error(f"Failed to process event: {e}")
            return False

    async def _read_events(self):
        """Read events from Redis stream and process them"""
        if not self.redis_client:
            logger.error("EventBus not connected")
            return

        while self.running:
            try:
                # Read new events from stream
                messages = await self.redis_client.xreadgroup(
                    self.consumer_group,
                    self.consumer_name,
                    {self.stream_name: '>'},  # Read only new messages
                    count=10,  # Read up to 10 messages at once
                    block=1000  # Wait up to 1 second for new messages
                )

                if messages and self.stream_name in messages:
                    for message_id, fields in messages[self.stream_name]:
                        # Convert Redis fields to dictionary
                        event_data = dict(fields)
                        await self._process_event(event_data)

            except Exception as e:
                if self.running:  # Only log error if we're still supposed to be running
                    logger.error(f"Error reading events: {e}")
                await asyncio.sleep(1)  # Wait before retrying

    async def start(self):
        """Start the EventBus event loop"""
        if not self.redis_client:
            await self.connect()

        self.running = True
        logger.info(f"Starting EventBus with consumer group {self.consumer_group}")

        # Start the event reading loop
        await self._read_events()

    async def stop(self):
        """Stop the EventBus event loop"""
        self.running = False
        logger.info("Stopping EventBus")

    async def get_recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent events from the stream

        Args:
            limit: Maximum number of events to retrieve

        Returns:
            List of event data dictionaries
        """
        if not self.redis_client:
            logger.error("EventBus not connected")
            return []

        try:
            # Read events from the end of the stream
            messages = await self.redis_client.xrevrange(
                self.stream_name,
                count=limit
            )

            events = []
            for message_id, fields in messages:
                event_data = dict(fields)
                events.append(event_data)

            return events
        except Exception as e:
            logger.error(f"Failed to get recent events: {e}")
            return []

    async def get_events_by_type(self, event_type: EventType, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get recent events of a specific type

        Args:
            event_type: The event type to filter by
            limit: Maximum number of events to retrieve

        Returns:
            List of event data dictionaries
        """
        all_events = await self.get_recent_events(limit * 10)  # Get more to account for filtering
        return [
            event for event in all_events
            if event.get("event_type") == event_type.value
        ][:limit]