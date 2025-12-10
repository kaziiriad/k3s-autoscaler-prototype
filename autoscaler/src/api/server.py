#!/usr/bin/env python3
"""
FastAPI server module for autoscaler API endpoints
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from core.autoscaler import K3sAutoscaler
from database import DatabaseManager

logger = logging.getLogger(__name__)

# Pydantic models for request/response
class ScalingRequest(BaseModel):
    action: str  # "scale_up" or "scale_down"
    count: int = 1
    reason: Optional[str] = None

class ScalingRuleRequest(BaseModel):
    name: str
    enabled: bool = True
    metric_name: str
    operator: str
    threshold: float
    scale_direction: str  # "up" or "down"
    scale_amount: int = 1
    cooldown_seconds: int = 60
    conditions: Dict[str, Any] = {}


class APIServer:
    """FastAPI server for autoscaler endpoints"""

    def __init__(self, autoscaler: K3sAutoscaler, config: Dict):
        """
        Initialize API server

        Args:
            autoscaler: K3sAutoscaler instance
            config: Configuration dictionary
        """
        self.autoscaler = autoscaler
        self.config = config
        self.app = FastAPI(
            title="K3s Autoscaler API",
            description="API for managing K3s cluster autoscaling",
            version="1.0.0"
        )
        self._setup_routes()

    def _setup_routes(self):
        """Setup API routes"""

        @self.app.get("/")
        async def root():
            """Root endpoint"""
            return {
                "service": "K3s Autoscaler",
                "version": "1.0.0",
                "timestamp": datetime.utcnow().isoformat()
            }

        @self.app.get("/health")
        async def health_check():
            """Health check endpoint"""
            try:
                status = self.autoscaler.get_status()
                db_health = status.get("database_health", {})

                # Check if all databases are healthy
                all_healthy = all(db_health.values())

                return JSONResponse(
                    content={
                        "status": "healthy" if all_healthy else "unhealthy",
                        "timestamp": datetime.utcnow().isoformat(),
                        "details": status
                    },
                    status_code=200 if all_healthy else 503
                )
            except Exception as e:
                logger.error(f"Health check error: {e}")
                return JSONResponse(
                    content={
                        "status": "unhealthy",
                        "error": str(e),
                        "timestamp": datetime.utcnow().isoformat()
                    },
                    status_code=503
                )

        @self.app.get("/status")
        async def get_status():
            """Get detailed autoscaler status"""
            try:
                status = self.autoscaler.get_status()
                return status
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/metrics")
        async def get_metrics():
            """Get current cluster metrics"""
            try:
                metrics_data = self.autoscaler.metrics.collect()
                return metrics_data
            except Exception as e:
                logger.error(f"Error getting metrics: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/workers")
        async def get_workers():
            """Get worker node details"""
            try:
                workers = self.autoscaler.get_worker_details()
                return {
                    "workers": workers,
                    "count": len(workers),
                    "timestamp": datetime.utcnow().isoformat()
                }
            except Exception as e:
                logger.error(f"Error getting workers: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/history")
        async def get_scaling_history(limit: int = 50):
            """Get scaling event history"""
            try:
                history = self.autoscaler.get_scaling_history(limit)
                return {
                    "events": [
                        {
                            "id": str(event.id),
                            "type": event.event_type.value,
                            "node_name": event.node_name,
                            "old_count": event.old_count,
                            "new_count": event.new_count,
                            "reason": event.reason,
                            "timestamp": event.timestamp.isoformat(),
                            "metrics": event.metrics,
                            "details": event.decision_details
                        }
                        for event in history
                    ],
                    "count": len(history),
                    "limit": limit
                }
            except Exception as e:
                logger.error(f"Error getting history: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/scale")
        async def manual_scale(request: ScalingRequest, background_tasks: BackgroundTasks):
            """Manually trigger scaling"""
            try:
                if request.action not in ["scale_up", "scale_down"]:
                    raise HTTPException(
                        status_code=400,
                        detail="Action must be 'scale_up' or 'scale_down'"
                    )

                # Execute scaling
                success = self.autoscaler._execute_scaling({
                    "action": request.action,
                    "count": request.count,
                    "reason": request.reason or "Manual scaling via API"
                })

                if success:
                    return {
                        "message": f"Successfully executed {request.action} by {request.count} nodes",
                        "action": request.action,
                        "count": request.count,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to execute {request.action}"
                    )

            except Exception as e:
                logger.error(f"Error in manual scaling: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/cycle")
        async def run_scaling_cycle(background_tasks: BackgroundTasks):
            """Trigger a scaling cycle"""
            try:
                # Run cycle in background
                background_tasks.add_task(self.autoscaler.run_cycle)

                return {
                    "message": "Scaling cycle triggered",
                    "timestamp": datetime.utcnow().isoformat()
                }
            except Exception as e:
                logger.error(f"Error triggering cycle: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/rules")
        async def get_scaling_rules():
            """Get scaling rules"""
            try:
                rules = self.autoscaler.database.get_scaling_rules()
                return {
                    "rules": [
                        {
                            "id": str(rule.id),
                            "name": rule.name,
                            "enabled": rule.enabled,
                            "metric_name": rule.metric_name,
                            "operator": rule.operator,
                            "threshold": rule.threshold,
                            "scale_direction": rule.scale_direction,
                            "scale_amount": rule.scale_amount,
                            "cooldown_seconds": rule.cooldown_seconds,
                            "conditions": rule.conditions,
                            "created_at": rule.created_at.isoformat() if rule.created_at else None,
                            "updated_at": rule.updated_at.isoformat() if rule.updated_at else None
                        }
                        for rule in rules
                    ],
                    "count": len(rules)
                }
            except Exception as e:
                logger.error(f"Error getting rules: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/rules")
        async def create_scaling_rule(request: ScalingRuleRequest):
            """Create a new scaling rule"""
            try:
                from ..database import ScalingRule

                rule = ScalingRule(
                    name=request.name,
                    enabled=request.enabled,
                    metric_name=request.metric_name,
                    operator=request.operator,
                    threshold=request.threshold,
                    scale_direction=request.scale_direction,
                    scale_amount=request.scale_amount,
                    cooldown_seconds=request.cooldown_seconds,
                    conditions=request.conditions,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )

                success = self.autoscaler.database.save_scaling_rule(rule)
                if success:
                    return {
                        "message": "Scaling rule created successfully",
                        "rule_id": str(rule.id),
                        "timestamp": datetime.utcnow().isoformat()
                    }
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Failed to create scaling rule"
                    )

            except Exception as e:
                logger.error(f"Error creating rule: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/config")
        async def get_config():
            """Get current autoscaler configuration (sanitized)"""
            try:
                # Return config without sensitive data
                safe_config = {
                    "autoscaler": {
                        "check_interval": self.config["autoscaler"]["check_interval"],
                        "dry_run": self.config["autoscaler"]["dry_run"],
                        "thresholds": self.config["autoscaler"]["thresholds"],
                        "limits": self.config["autoscaler"]["limits"]
                    }
                }
                return safe_config
            except Exception as e:
                logger.error(f"Error getting config: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/stop")
        async def stop_autoscaler():
            """Stop the autoscaler"""
            try:
                self.autoscaler.stop()
                return {
                    "message": "Autoscaler stopped",
                    "timestamp": datetime.utcnow().isoformat()
                }
            except Exception as e:
                logger.error(f"Error stopping autoscaler: {e}")
                raise HTTPException(status_code=500, detail=str(e))

    def run(self, host: str = "0.0.0.0", port: int = 8080):
        """Run the API server"""
        logger.info(f"Starting API server on {host}:{port}")
        uvicorn.run(self.app, host=host, port=port, log_level="info")