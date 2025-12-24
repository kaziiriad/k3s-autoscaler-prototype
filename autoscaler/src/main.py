#!/usr/bin/env python3
"""
K3s Docker Autoscaler - Main Entry Point
Autoscales k3s cluster by adding/removing Docker containers based on Prometheus metrics
"""

import os
import sys
import time
import signal
import logging
from typing import Dict, Optional
from prometheus_client import start_http_server, Gauge, Counter, Histogram
from datetime import datetime

# Add src to path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import centralized logging
from core.logging_config import setup_logging, get_logger

# Import modular components
from core.autoscaler import K3sAutoscaler
from core.reconciliation import ReconciliationService
from database import DatabaseManager
from api.server import APIServer
from config import Settings, settings

# Prometheus metrics
SCALING_DECISIONS = Counter('autoscaler_scaling_decisions_total', 'Total scaling decisions', ['decision'])
CURRENT_NODES = Gauge('autoscaler_current_nodes', 'Current number of k3s nodes')
PENDING_PODS = Gauge('autoscaler_pending_pods', 'Number of pending pods')
SCALE_UP_EVENTS = Counter('autoscaler_scale_up_events_total', 'Total scale-up events')
SCALE_DOWN_EVENTS = Counter('autoscaler_scale_down_events_total', 'Total scale-down events')
ERRORS = Counter('autoscaler_errors_total', 'Total errors', ['type'])

# Reconciliation metrics are defined locally in reconciliation.py to avoid duplication


class AutoscalerService:
    """Main autoscaler service that coordinates all components"""

    def __init__(self, config_path: Optional[str] = None):
        """Initialize the autoscaler service"""
        # Load settings using Pydantic
        if config_path and os.path.exists(config_path):
            self.settings = Settings.load_from_yaml_with_env_override(config_path)
        else:
            self.settings = Settings()

        # Convert to config dict for compatibility
        self.config = self.settings.get_config_dict()

        # Setup logging
        setup_logging(
            level=self.config.get('logging', {}).get('level', 'INFO'),
            log_file=self.config.get('logging', {}).get('file', '/app/logs/autoscaler.log'),
            enable_colors=True
        )
        self.logger = get_logger(__name__)
        self.running = True

        # Initialize database manager
        self.database = self._init_database()

        # Initialize autoscaler
        self.autoscaler = K3sAutoscaler(self.config, self.database)

        # Initialize API server
        self.api_server = APIServer(self.autoscaler, self.config, service=self)

        # Initialize reconciliation service
        self.reconciliation_service = None

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self.logger.info("K3s Autoscaler Service initialized")
        if self.settings.debug:
            self.logger.info(f"Debug mode enabled. Settings: {self.settings.dict()}")

    def _init_database(self) -> DatabaseManager:
        """Initialize database manager"""
        try:
            db_config = self.config['autoscaler']['database']
            database = DatabaseManager(
                mongodb_url=db_config['mongodb']['url'],
                database_name=db_config['mongodb']['database_name'],
                redis_host=db_config['redis']['host'],
                redis_port=db_config['redis']['port'],
                redis_db=db_config['redis']['db'],
                redis_password=db_config['redis'].get('password')
            )

            # Initialize default scaling rules
            database.initialize_default_rules()

            self.logger.info("Database manager initialized successfully")
            return database

        except Exception as e:
            self.logger.error(f"Failed to initialize database: {e}")
            sys.exit(1)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.running = False
        if self.autoscaler:
            self.autoscaler.stop()

    def _update_prometheus_metrics(self, metrics: Dict):
        """Update Prometheus metrics with latest data"""
        try:
            CURRENT_NODES.set(metrics.get('current_nodes', 0))
            PENDING_PODS.set(metrics.get('pending_pods', 0))

            # Track scaling decisions
            if metrics.get('decision', {}).get('action'):
                action = metrics['decision']['action']
                SCALING_DECISIONS.labels(decision=action).inc()

                if action == 'scale_up':
                    SCALE_UP_EVENTS.inc()
                elif action == 'scale_down':
                    SCALE_DOWN_EVENTS.inc()

        except Exception as e:
            self.logger.error(f"Error updating Prometheus metrics: {e}")
            ERRORS.labels(type='prometheus').inc()

    def run(self):
        """Main run loop"""
        self.logger.info("Starting K3s Autoscaler Service...")

        # Start Prometheus metrics server
        start_http_server(9091)
        self.logger.info("Prometheus metrics server started on :9091")

        # Initialize scale_down metric for visibility
        SCALING_DECISIONS.labels(decision="scale_down").inc(1)

        # Start API server in background
        import threading
        api_thread = threading.Thread(
            target=self.api_server.run,
            kwargs={'host': '0.0.0.0', 'port': 8080}
        )
        api_thread.daemon = True
        api_thread.start()
        self.logger.info("API server started on :8080")

        # Start reconciliation service in background
        import asyncio
        async def start_background_services():
            # Start reconciliation
            self.reconciliation_service = ReconciliationService(self.autoscaler)
            await self.reconciliation_service.start()

        # Run background services
        background_thread = threading.Thread(
            target=lambda: asyncio.run(start_background_services())
        )
        background_thread.daemon = True
        background_thread.start()
        self.logger.info("Background services started (reconciliation)")

        # Main autoscaling loop
        interval = self.config['autoscaler']['check_interval']
        self.logger.info(f"Starting autoscaling loop with {interval}s interval")

        # Track cleanup cycles
        cleanup_counter = 0
        cleanup_interval = 60  # Run cleanup every 60 cycles (5 minutes if interval=5s)

        while self.running:
            try:
                # Run autoscaling cycle
                result = self.autoscaler.run_cycle()

                if 'error' not in result:
                    # Update Prometheus metrics
                    self._update_prometheus_metrics(result)

                    # Log decision
                    decision = result.get('decision', {})
                    if decision.get('should_scale'):
                        action = decision.get('action', 'unknown')
                        reason = decision.get('reason', 'unknown')
                        self.logger.info(f"Scaling action: {action} - {reason}")

                else:
                    self.logger.error(f"Autoscaling cycle error: {result['error']}")
                    ERRORS.labels(type='autoscaling_cycle').inc()

                # Periodic cleanup of orphaned nodes
                cleanup_counter += 1
                if cleanup_counter >= cleanup_interval:
                    self.logger.info("Running periodic cleanup of orphaned Kubernetes nodes...")
                    cleanup_result = self.autoscaler.cleanup_orphaned_nodes()
                    if cleanup_result['cleaned_nodes']:
                        self.logger.info(f"Cleaned {len(cleanup_result['cleaned_nodes'])} orphaned nodes")
                    cleanup_counter = 0

            except Exception as e:
                self.logger.error(f"Unexpected error in autoscaling loop: {e}")
                ERRORS.labels(type='unexpected').inc()

            # Sleep until next cycle
            time.sleep(interval)

        self.logger.info("Autoscaler service stopped")

    def cleanup(self):
        """Cleanup resources"""
        try:
            if self.autoscaler:
                self.autoscaler.cleanup()
            if self.database:
                self.database.close()
            self.logger.info("Cleanup completed")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")


def main():
    """Entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='K3s Docker Autoscaler')
    parser.add_argument(
        '--config',
        default=os.getenv('CONFIG_PATH', '/app/config/config-with-db.yaml'),
        help='Path to configuration file'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Run in dry-run mode (no actual scaling)'
    )

    args = parser.parse_args()

    # Create and run autoscaler service
    service = AutoscalerService(args.config)

    # Override dry-run setting if specified
    if args.dry_run:
        service.config['autoscaler']['dry_run'] = True
        service.logger.info("Dry-run mode enabled")

    try:
        service.run()
    except KeyboardInterrupt:
        service.logger.info("Received keyboard interrupt")
    except Exception as e:
        service.logger.error(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        service.cleanup()


if __name__ == "__main__":
    main()