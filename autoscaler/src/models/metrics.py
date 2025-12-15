#!/usr/bin/env python3
"""
Pydantic models for metrics and related data structures
"""

from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class NodeMetrics(BaseModel):
    """Individual node metrics"""
    node_name: str = Field(..., description="Name of the node")
    instance: str = Field(..., description="Instance identifier (e.g., 'k3s-master:9100')")
    cpu_usage: float = Field(..., ge=0, le=100, description="CPU usage percentage")
    memory_usage: float = Field(..., ge=0, le=100, description="Memory usage percentage")

    # Optional detailed memory metrics
    memory_used_bytes: Optional[int] = Field(None, ge=0, description="Memory used in bytes")
    memory_total_bytes: Optional[int] = Field(None, ge=0, description="Total memory in bytes")
    memory_available_bytes: Optional[int] = Field(None, ge=0, description="Available memory in bytes")

    # Status information
    ready: Optional[bool] = Field(None, description="Whether the node is ready")
    roles: Optional[list[str]] = Field(None, description="Node roles")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class ClusterMetrics(BaseModel):
    """Overall cluster metrics"""
    # Node counts
    current_nodes: int = Field(..., ge=0, description="Current number of worker nodes")
    ready_nodes: int = Field(..., ge=0, description="Number of ready nodes")
    pending_pods: int = Field(..., ge=0, description="Number of pending pods")

    # Average resource usage
    avg_cpu: float = Field(..., ge=0, le=100, description="Average CPU usage percentage")
    avg_memory: float = Field(..., ge=0, le=100, description="Average memory usage percentage")

    # Resource allocation (from kube-state-metrics)
    total_cpu: float = Field(0.0, ge=0, description="Total requested CPU cores")
    total_memory: float = Field(0.0, ge=0, description="Total requested memory bytes")
    allocatable_cpu: float = Field(0.0, ge=0, description="Total allocatable CPU cores")
    allocatable_memory: float = Field(0.0, ge=0, description="Total allocatable memory bytes")

    # Converted values for readability
    total_memory_gb: Optional[float] = Field(None, ge=0, description="Total requested memory in GB")
    allocatable_memory_gb: Optional[float] = Field(None, ge=0, description="Total allocatable memory in GB")

    # Timestamp
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Timestamp of metrics collection")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class ClusterCapacity(BaseModel):
    """Cluster resource capacity information"""
    cpu_cores: float = Field(..., ge=0, description="Total CPU cores in the cluster")
    memory_bytes: int = Field(..., ge=0, description="Total memory in bytes")
    memory_gb: float = Field(..., ge=0, description="Total memory in GB")
    pods_capacity: int = Field(..., ge=0, description="Total pod capacity")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class MetricsResponse(BaseModel):
    """Response model for metrics API endpoints"""
    metrics: ClusterMetrics
    nodes: Dict[str, NodeMetrics] = Field(default_factory=dict, description="Individual node metrics")
    capacity: Optional[ClusterCapacity] = Field(None, description="Cluster capacity information")

    # Additional metadata
    count: int = Field(..., ge=0, description="Number of nodes with metrics")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Response timestamp")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class ScalingDecision(BaseModel):
    """Model for scaling decisions"""
    should_scale: bool = Field(..., description="Whether scaling should occur")
    action: Optional[str] = Field(None, description="Scale action: 'scale_up' or 'scale_down'")
    count: int = Field(..., ge=0, description="Number of nodes to scale")
    reason: str = Field(..., description="Reason for the scaling decision")

    # Metrics that triggered the decision
    trigger_cpu: Optional[float] = Field(None, description="CPU usage that triggered scaling")
    trigger_memory: Optional[float] = Field(None, description="Memory usage that triggered scaling")
    trigger_pending_pods: Optional[int] = Field(None, description="Pending pod count that triggered scaling")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class HealthStatus(BaseModel):
    """Health check response model"""
    status: str = Field(..., description="Health status: 'healthy' or 'unhealthy'")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Health check timestamp")

    # Component health
    database_health: Optional[Dict[str, bool]] = Field(None, description="Database component health")
    prometheus_connected: Optional[bool] = Field(None, description="Whether Prometheus is connected")

    # Additional details
    details: Optional[Dict[str, Any]] = Field(None, description="Additional health details")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }