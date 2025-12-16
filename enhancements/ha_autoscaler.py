#!/usr/bin/env python3
"""
High Availability Autoscaler with Leader Election
Enables running multiple autoscaler instances with active-passive failover
"""

import logging
import time
import socket
from datetime import datetime, timedelta
from typing import Optional
import redis

logger = logging.getLogger(__name__)


class LeaderElection:
    """
    Distributed leader election using Redis
    Ensures only one autoscaler instance is active at a time
    """
    
    def __init__(self, redis_client: redis.Redis, 
                 lock_name: str = "autoscaler:leader",
                 ttl_seconds: int = 10):
        """
        Initialize leader election
        
        Args:
            redis_client: Redis client instance
            lock_name: Name of the leader lock
            ttl_seconds: Time-to-live for the leader lock
        """
        self.redis = redis_client
        self.lock_name = lock_name
        self.ttl_seconds = ttl_seconds
        self.instance_id = f"{socket.gethostname()}-{int(time.time())}"
        self.is_leader = False
        
    def try_acquire_leadership(self) -> bool:
        """
        Attempt to acquire leadership
        
        Returns:
            bool: True if leadership acquired, False otherwise
        """
        try:
            # Try to set the lock with NX (only if not exists)
            acquired = self.redis.set(
                self.lock_name,
                self.instance_id,
                nx=True,
                ex=self.ttl_seconds
            )
            
            if acquired:
                self.is_leader = True
                logger.info(f"🎖️  Leadership acquired by {self.instance_id}")
                return True
            
            # Check if we already own the lock
            current_leader = self.redis.get(self.lock_name)
            if current_leader and current_leader.decode() == self.instance_id:
                # Renew our leadership
                self.redis.expire(self.lock_name, self.ttl_seconds)
                self.is_leader = True
                return True
            
            self.is_leader = False
            return False
            
        except Exception as e:
            logger.error(f"Error during leader election: {e}")
            self.is_leader = False
            return False
    
    def renew_leadership(self) -> bool:
        """
        Renew leadership lock to prevent expiration
        
        Returns:
            bool: True if renewal successful, False otherwise
        """
        if not self.is_leader:
            return False
        
        try:
            # Verify we still own the lock
            current_leader = self.redis.get(self.lock_name)
            if not current_leader or current_leader.decode() != self.instance_id:
                logger.warning(f"Lost leadership to {current_leader}")
                self.is_leader = False
                return False
            
            # Renew the lock
            self.redis.expire(self.lock_name, self.ttl_seconds)
            logger.debug(f"Leadership renewed by {self.instance_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error renewing leadership: {e}")
            self.is_leader = False
            return False
    
    def release_leadership(self):
        """Release leadership lock"""
        try:
            # Only release if we own the lock
            current_leader = self.redis.get(self.lock_name)
            if current_leader and current_leader.decode() == self.instance_id:
                self.redis.delete(self.lock_name)
                logger.info(f"Leadership released by {self.instance_id}")
            self.is_leader = False
        except Exception as e:
            logger.error(f"Error releasing leadership: {e}")
    
    def get_current_leader(self) -> Optional[str]:
        """Get the current leader instance ID"""
        try:
            leader = self.redis.get(self.lock_name)
            return leader.decode() if leader else None
        except Exception as e:
            logger.error(f"Error getting current leader: {e}")
            return None


class HAAutoscalerService:
    """
    High Availability Autoscaler Service
    Wraps the autoscaler with leader election
    """
    
    def __init__(self, autoscaler, redis_client: redis.Redis):
        """
        Initialize HA Autoscaler Service
        
        Args:
            autoscaler: The core autoscaler instance
            redis_client: Redis client for leader election
        """
        self.autoscaler = autoscaler
        self.leader_election = LeaderElection(redis_client)
        self.running = True
        
        # Health check interval (faster than autoscaler cycle)
        self.health_check_interval = 5  # seconds
        
    def run(self):
        """
        Main run loop with leader election
        """
        logger.info(f"Starting HA Autoscaler (Instance: {self.leader_election.instance_id})")
        
        while self.running:
            try:
                # Try to acquire or renew leadership
                if self.leader_election.is_leader:
                    # We are the leader, renew the lock
                    if not self.leader_election.renew_leadership():
                        logger.warning("Lost leadership, switching to follower mode")
                        continue
                else:
                    # We are a follower, try to become leader
                    if not self.leader_election.try_acquire_leadership():
                        # Still a follower, wait and retry
                        logger.debug(
                            f"Running as follower. Current leader: "
                            f"{self.leader_election.get_current_leader()}"
                        )
                        time.sleep(self.health_check_interval)
                        continue
                
                # We are the leader, run autoscaler cycle
                logger.debug("Running autoscaler cycle as leader")
                self.autoscaler.run_cycle()
                
                # Sleep until next cycle
                time.sleep(self.autoscaler.config['autoscaler']['check_interval'])
                
            except KeyboardInterrupt:
                logger.info("Received shutdown signal")
                self.stop()
                break
            except Exception as e:
                logger.error(f"Error in HA autoscaler loop: {e}")
                time.sleep(self.health_check_interval)
    
    def stop(self):
        """Stop the HA autoscaler"""
        logger.info("Stopping HA autoscaler...")
        self.running = False
        
        # Release leadership if we have it
        if self.leader_election.is_leader:
            self.leader_election.release_leadership()
        
        # Stop the autoscaler
        if self.autoscaler:
            self.autoscaler.stop()
    
    def get_status(self) -> dict:
        """Get HA autoscaler status"""
        return {
            "instance_id": self.leader_election.instance_id,
            "is_leader": self.leader_election.is_leader,
            "current_leader": self.leader_election.get_current_leader(),
            "autoscaler_status": self.autoscaler.get_status() if self.autoscaler else None
        }


# Usage in main.py:
# from core.ha_autoscaler import HAAutoscalerService
#
# # Create Redis client for leader election
# redis_client = redis.Redis(
#     host=config['autoscaler']['database']['redis']['host'],
#     port=config['autoscaler']['database']['redis']['port'],
#     decode_responses=True
# )
#
# # Wrap autoscaler with HA service
# ha_service = HAAutoscalerService(autoscaler, redis_client)
# ha_service.run()
