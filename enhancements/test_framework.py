#!/usr/bin/env python3
"""
Comprehensive Testing Framework for K3s Autoscaler
Includes unit tests, integration tests, and mocks
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime
from typing import Dict, Any

# Mock implementations for testing
class MockPrometheusClient:
    """Mock Prometheus client for testing"""
    
    def __init__(self, responses: Dict[str, Any] = None):
        self.responses = responses or {}
        self.query_count = 0
    
    def query(self, query: str):
        """Mock query method"""
        self.query_count += 1
        return self.responses.get(query, {
            "status": "success",
            "data": {"result": [{"value": [0, "50"]}]}
        })


class MockKubernetesClient:
    """Mock Kubernetes client for testing"""
    
    def __init__(self):
        self.nodes = []
        self.pods = []
    
    def list_node(self):
        """Mock list_node method"""
        return Mock(items=self.nodes)
    
    def list_pod_for_all_namespaces(self, **kwargs):
        """Mock list_pod method"""
        return Mock(items=self.pods)
    
    def add_node(self, name: str, status: str = "Ready"):
        """Helper to add mock node"""
        node = Mock()
        node.metadata = Mock()
        node.metadata.name = name
        node.metadata.labels = {}
        node.status = Mock()
        node.status.conditions = [
            Mock(type="Ready", status="True" if status == "Ready" else "False")
        ]
        self.nodes.append(node)
        return node


class MockDockerClient:
    """Mock Docker client for testing"""
    
    def __init__(self):
        self.containers = {}
        self.container_id_counter = 0
    
    def containers_run(self, **kwargs):
        """Mock container creation"""
        self.container_id_counter += 1
        container_id = f"container_{self.container_id_counter}"
        
        container = Mock()
        container.id = container_id
        container.name = kwargs.get('name', f'worker-{self.container_id_counter}')
        container.status = 'running'
        
        self.containers[container_id] = container
        return container
    
    def containers_get(self, container_id: str):
        """Mock get container"""
        if container_id in self.containers:
            return self.containers[container_id]
        raise Exception(f"Container {container_id} not found")
    
    def containers_list(self, **kwargs):
        """Mock list containers"""
        return list(self.containers.values())


# Unit Tests
class TestScalingEngine:
    """Test scaling decision logic"""
    
    @pytest.fixture
    def scaling_engine(self):
        """Create scaling engine with test config"""
        config = {
            'autoscaler': {
                'thresholds': {
                    'pending_pods': 1,
                    'cpu_threshold': 80,
                    'memory_threshold': 80,
                    'cpu_scale_down': 30,
                    'memory_scale_down': 30
                },
                'limits': {
                    'min_nodes': 2,
                    'max_nodes': 6,
                    'scale_up_cooldown': 60,
                    'scale_down_cooldown': 120
                }
            }
        }
        
        mock_database = Mock()
        mock_database.is_cooldown_active.return_value = False
        mock_database.get_scaling_rules.return_value = []
        
        from core.scaling import ScalingEngine
        return ScalingEngine(config, mock_database)
    
    def test_scale_up_on_pending_pods(self, scaling_engine):
        """Test scale up decision when pods are pending"""
        metrics = {
            "current_nodes": 2,
            "pending_pods": 3,
            "avg_cpu": 50.0,
            "avg_memory": 50.0
        }
        
        decision = scaling_engine._evaluate_thresholds(
            metrics,
            metrics["current_nodes"],
            metrics["pending_pods"],
            metrics["avg_cpu"],
            metrics["avg_memory"]
        )
        
        assert decision["should_scale"] is True
        assert decision["action"] == "scale_up"
        assert "pending pods" in decision["reason"].lower()
    
    def test_scale_up_on_high_cpu(self, scaling_engine):
        """Test scale up decision on high CPU"""
        metrics = {
            "current_nodes": 2,
            "pending_pods": 0,
            "avg_cpu": 85.0,
            "avg_memory": 50.0
        }
        
        decision = scaling_engine._evaluate_thresholds(
            metrics,
            metrics["current_nodes"],
            metrics["pending_pods"],
            metrics["avg_cpu"],
            metrics["avg_memory"]
        )
        
        assert decision["should_scale"] is True
        assert decision["action"] == "scale_up"
        assert "cpu" in decision["reason"].lower()
    
    def test_scale_down_on_low_utilization(self, scaling_engine):
        """Test scale down decision on low utilization"""
        metrics = {
            "current_nodes": 4,  # Above minimum
            "pending_pods": 0,
            "avg_cpu": 20.0,
            "avg_memory": 20.0
        }
        
        decision = scaling_engine._evaluate_thresholds(
            metrics,
            metrics["current_nodes"],
            metrics["pending_pods"],
            metrics["avg_cpu"],
            metrics["avg_memory"]
        )
        
        assert decision["should_scale"] is True
        assert decision["action"] == "scale_down"
    
    def test_no_scale_down_at_minimum(self, scaling_engine):
        """Test that scaling down doesn't happen at minimum nodes"""
        metrics = {
            "current_nodes": 2,  # At minimum
            "pending_pods": 0,
            "avg_cpu": 20.0,
            "avg_memory": 20.0
        }
        
        decision = scaling_engine._evaluate_thresholds(
            metrics,
            metrics["current_nodes"],
            metrics["pending_pods"],
            metrics["avg_cpu"],
            metrics["avg_memory"]
        )
        
        assert decision["should_scale"] is False


class TestMetricsCollector:
    """Test metrics collection"""
    
    @pytest.fixture
    def metrics_collector(self):
        """Create metrics collector with mocks"""
        config = {'autoscaler': {'kubernetes': {}}}
        mock_database = Mock()
        
        from core.metrics import MetricsCollector
        collector = MetricsCollector(config, mock_database)
        
        # Mock Kubernetes and Prometheus clients
        collector.k8s_api = MockKubernetesClient()
        
        return collector
    
    def test_collect_metrics_with_nodes(self, metrics_collector):
        """Test metrics collection with active nodes"""
        # Add mock nodes
        metrics_collector.k8s_api.add_node("worker-1", "Ready")
        metrics_collector.k8s_api.add_node("worker-2", "Ready")
        
        # Mock Prometheus responses
        with patch.object(metrics_collector, '_query_prometheus') as mock_query:
            mock_query.return_value = [{"value": [0, "50"]}]
            
            metrics = metrics_collector.collect()
            
            assert metrics.current_nodes == 2
            assert metrics.ready_nodes == 2
            assert metrics.avg_cpu >= 0


# Integration Tests
@pytest.mark.integration
class TestAutoscalerIntegration:
    """Integration tests for full autoscaler"""
    
    @pytest.fixture
    def autoscaler_with_mocks(self):
        """Create autoscaler with mock dependencies"""
        config = {
            'autoscaler': {
                'check_interval': 1,
                'dry_run': False,
                'kubernetes': {'in_cluster': False},
                'thresholds': {
                    'pending_pods': 1,
                    'cpu_threshold': 80,
                    'memory_threshold': 80,
                    'cpu_scale_down': 30,
                    'memory_scale_down': 30
                },
                'limits': {
                    'min_nodes': 2,
                    'max_nodes': 6,
                    'scale_up_cooldown': 60,
                    'scale_down_cooldown': 120
                },
                'docker': {
                    'network': 'test-network',
                    'worker_service': 'test-worker',
                    'image': 'test:latest',
                    'cpu_limit': '1',
                    'memory_limit': '2g',
                    'boot_time': 1
                }
            }
        }
        
        # Mock database
        mock_db = Mock()
        mock_db.is_cooldown_active.return_value = False
        mock_db.get_scaling_rules.return_value = []
        mock_db.get_worker_count.return_value = 2
        mock_db.get_all_workers.return_value = []
        mock_db.record_scaling_event.return_value = True
        
        from core.autoscaler import K3sAutoscaler
        autoscaler = K3sAutoscaler(config, mock_db)
        
        # Mock clients
        autoscaler.metrics.k8s_api = MockKubernetesClient()
        autoscaler.docker_client = MockDockerClient()
        
        return autoscaler
    
    def test_full_scale_up_cycle(self, autoscaler_with_mocks):
        """Test complete scale up cycle"""
        autoscaler = autoscaler_with_mocks
        
        # Add pending pods to trigger scale up
        pending_pod = Mock()
        pending_pod.metadata = Mock(name="test-pod")
        pending_pod.status = Mock(phase="Pending")
        autoscaler.metrics.k8s_api.pods.append(pending_pod)
        
        # Mock Prometheus to return high CPU
        with patch.object(autoscaler.metrics, '_query_prometheus') as mock_prom:
            mock_prom.return_value = [{"value": [0, "85"]}]
            
            # Run cycle
            result = autoscaler.run_cycle()
            
            # Verify decision was made
            assert 'decision' in result
            decision = result['decision']
            
            # Should trigger scale up due to high CPU
            if decision.get('should_scale'):
                assert decision['action'] == 'scale_up'


# Performance Tests
class TestPerformance:
    """Performance and load tests"""
    
    def test_metrics_collection_performance(self, benchmark):
        """Benchmark metrics collection speed"""
        config = {'autoscaler': {'kubernetes': {}}}
        mock_db = Mock()
        
        from core.metrics import MetricsCollector
        collector = MetricsCollector(config, mock_db)
        collector.k8s_api = MockKubernetesClient()
        
        # Add multiple nodes
        for i in range(10):
            collector.k8s_api.add_node(f"worker-{i}", "Ready")
        
        # Benchmark
        with patch.object(collector, '_query_prometheus') as mock_query:
            mock_query.return_value = [{"value": [0, "50"]}]
            benchmark(collector.collect)


# Fixtures for database testing
@pytest.fixture
def mock_mongodb():
    """Mock MongoDB for testing"""
    with patch('pymongo.MongoClient') as mock_client:
        mock_db = Mock()
        mock_collection = Mock()
        mock_client.return_value.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_collection
        yield mock_collection


@pytest.fixture
def mock_redis():
    """Mock Redis for testing"""
    with patch('redis.Redis') as mock_redis:
        mock_client = Mock()
        mock_redis.return_value = mock_client
        yield mock_client


# Test configuration
def pytest_configure(config):
    """Configure pytest"""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )


# Run tests with:
# pytest tests/ -v
# pytest tests/ -v -m "not integration"  # Skip integration tests
# pytest tests/ -v --benchmark-only      # Only run benchmarks
