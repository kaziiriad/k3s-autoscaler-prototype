#!/usr/bin/env python3
"""
Configuration settings using Pydantic for environment variable loading
"""

import os
from typing import Optional, Dict, Any

# Try Pydantic v2 first, fallback to v1
try:
    from pydantic import Field, validator
    from pydantic_settings import BaseSettings
except ImportError:
    # Fallback to Pydantic v1
    from pydantic.v1 import BaseSettings, Field, validator

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, that's OK

class MongoDBSettings(BaseSettings):
    """MongoDB configuration settings"""
    url: str = Field(default="mongodb://localhost:27017", env="MONGODB_URL")
    database_name: str = Field(default="autoscaler", env="MONGODB_DATABASE_NAME")
    connection_timeout: int = Field(default=5, env="MONGODB_CONNECTION_TIMEOUT")

    model_config = {"env_prefix": "MONGODB_", "extra": "ignore"}


class RedisSettings(BaseSettings):
    """Redis configuration settings"""
    host: str = Field(default="localhost", env="REDIS_HOST")
    port: int = Field(default=6379, env="REDIS_PORT")
    db: int = Field(default=0, env="REDIS_DB")
    password: Optional[str] = Field(default=None, env="REDIS_PASSWORD")
    connection_timeout: int = Field(default=5, env="REDIS_CONNECTION_TIMEOUT")

    model_config = {"env_prefix": "REDIS_", "extra": "ignore"}


class KubernetesSettings(BaseSettings):
    """Kubernetes configuration settings"""
    in_cluster: bool = Field(default=False, env="KUBERNETES_IN_CLUSTER")
    kubeconfig_path: str = Field(default="/config/kubeconfig", env="KUBECONFIG_PATH")
    server_host: Optional[str] = Field(default=None, env="K3S_SERVER_HOST")

    model_config = {"env_prefix": "KUBERNETES_", "extra": "ignore"}


class DockerSettings(BaseSettings):
    """Docker configuration settings"""
    network: str = Field(default="prototype_k3s-network", env="DOCKER_NETWORK")
    worker_service: str = Field(default="k3s-worker", env="DOCKER_WORKER_SERVICE")
    image: str = Field(default="rancher/k3s:v1.29.1-k3s1", env="DOCKER_IMAGE")
    cpu_limit: str = Field(default="1", env="DOCKER_CPU_LIMIT")
    memory_limit: str = Field(default="2g", env="DOCKER_MEMORY_LIMIT")
    api_delay: int = Field(default=3, env="DOCKER_API_DELAY")
    boot_time: int = Field(default=30, env="DOCKER_BOOT_TIME")

    model_config = {"env_prefix": "DOCKER_", "extra": "ignore"}


class AutoscalerSettings(BaseSettings):
    """Main autoscaler configuration settings"""
    check_interval: int = Field(default=5, env="AUTOSCALER_CHECK_INTERVAL")
    dry_run: bool = Field(default=False, env="AUTOSCALER_DRY_RUN")

    # API settings
    api_host: str = Field(default="0.0.0.0", env="AUTOSCALER_API_HOST")
    api_port: int = Field(default=8080, env="AUTOSCALER_API_PORT")
    metrics_port: int = Field(default=9091, env="AUTOSCALER_METRICS_PORT")

    # Threshold settings
    pending_pods_threshold: int = Field(default=1, env="AUTOSCALER_PENDING_PODS_THRESHOLD")
    cpu_threshold_up: float = Field(default=80.0, env="AUTOSCALER_CPU_THRESHOLD_UP")
    memory_threshold_up: float = Field(default=80.0, env="AUTOSCALER_MEMORY_THRESHOLD_UP")
    cpu_threshold_down: float = Field(default=30.0, env="AUTOSCALER_CPU_THRESHOLD_DOWN")
    memory_threshold_down: float = Field(default=30.0, env="AUTOSCALER_MEMORY_THRESHOLD_DOWN")

    # Limit settings
    min_nodes: int = Field(default=2, env="AUTOSCALER_MIN_NODES")
    max_nodes: int = Field(default=6, env="AUTOSCALER_MAX_NODES")
    scale_up_cooldown: int = Field(default=60, env="AUTOSCALER_SCALE_UP_COOLDOWN")
    scale_down_cooldown: int = Field(default=120, env="AUTOSCALER_SCALE_DOWN_COOLDOWN")

    # Token
    k3s_token: str = Field(default="mysupersecrettoken12345", env="K3S_TOKEN")

    model_config = {
        "env_prefix": "AUTOSCALER_",
        "extra": "ignore"  # Allow extra fields from YAML config
    }


class LoggingSettings(BaseSettings):
    """Logging configuration settings"""
    level: str = Field(default="INFO", env="LOG_LEVEL")
    format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        env="LOG_FORMAT"
    )
    file: Optional[str] = Field(default=None, env="LOG_FILE")

    model_config = {"env_prefix": "LOG_"}


class PrometheusSettings(BaseSettings):
    """Prometheus configuration settings"""
    url: str = Field(default="http://prometheus:9090", env="PROMETHEUS_URL")
    query_timeout: int = Field(default=5, env="PROMETHEUS_QUERY_TIMEOUT")

    model_config = {"env_prefix": "PROMETHEUS_"}


class GrafanaSettings(BaseSettings):
    """Grafana configuration settings"""
    admin_password: str = Field(default="admin", env="GF_SECURITY_ADMIN_PASSWORD")

    model_config = {"env_prefix": "GF_"}


class Settings(BaseSettings):
    """Main settings class that includes all sub-settings"""
    # Environment
    environment: str = Field(default="development", env="ENVIRONMENT")
    debug: bool = Field(default=False, env="DEBUG")

    # Component settings
    mongodb: MongoDBSettings = Field(default_factory=MongoDBSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    docker: DockerSettings = Field(default_factory=DockerSettings)
    autoscaler: AutoscalerSettings = Field(default_factory=AutoscalerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    prometheus: PrometheusSettings = Field(default_factory=PrometheusSettings)
    grafana: GrafanaSettings = Field(default_factory=GrafanaSettings)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore"  # Allow extra fields to avoid validation errors
    }

    def get_config_dict(self) -> Dict[str, Any]:
        """Convert settings to a dictionary format compatible with the existing autoscaler"""
        return {
            "autoscaler": {
                "check_interval": self.autoscaler.check_interval,
                "dry_run": self.autoscaler.dry_run,
                "kubernetes": {
                    "in_cluster": self.kubernetes.in_cluster,
                    "kubeconfig_path": self.kubernetes.kubeconfig_path,
                    "server_host": self.kubernetes.server_host or "k3s-master"
                },
                "thresholds": {
                    "pending_pods": self.autoscaler.pending_pods_threshold,
                    "cpu_threshold": self.autoscaler.cpu_threshold_up,
                    "memory_threshold": self.autoscaler.memory_threshold_up,
                    "cpu_scale_down": self.autoscaler.cpu_threshold_down,
                    "memory_scale_down": self.autoscaler.memory_threshold_down
                },
                "limits": {
                    "min_nodes": self.autoscaler.min_nodes,
                    "max_nodes": self.autoscaler.max_nodes,
                    "scale_up_cooldown": self.autoscaler.scale_up_cooldown,
                    "scale_down_cooldown": self.autoscaler.scale_down_cooldown
                },
                "docker": {
                    "network": self.docker.network,
                    "worker_service": self.docker.worker_service,
                    "image": self.docker.image,
                    "cpu_limit": self.docker.cpu_limit,
                    "memory_limit": self.docker.memory_limit,
                    "api_delay": self.docker.api_delay,
                    "boot_time": self.docker.boot_time
                },
                "database": {
                    "mongodb": {
                        "url": self.mongodb.url,
                        "database_name": self.mongodb.database_name,
                        "connection_timeout": self.mongodb.connection_timeout
                    },
                    "redis": {
                        "host": self.redis.host,
                        "port": self.redis.port,
                        "db": self.redis.db,
                        "password": self.redis.password,
                        "connection_timeout": self.redis.connection_timeout
                    }
                }
            },
            "logging": {
                "level": self.logging.level,
                "format": self.logging.format,
                "file": self.logging.file
            },
            "prometheus": {
                "url": self.prometheus.url,
                "query_timeout": self.prometheus.query_timeout
            },
            "api": {
                "host": self.autoscaler.api_host,
                "port": self.autoscaler.api_port,
                "metrics_port": self.autoscaler.metrics_port
            },
            "k3s": {
                "token": self.autoscaler.k3s_token
            }
        }

    @classmethod
    def load_from_yaml_with_env_override(cls, yaml_path: str) -> "Settings":
        """Load settings from YAML file and override with environment variables"""
        import yaml

        # Load YAML if it exists
        yaml_config = {}
        if os.path.exists(yaml_path):
            with open(yaml_path, 'r') as f:
                # Process environment variables in YAML
                yaml_content = f.read()
                for key, value in os.environ.items():
                    yaml_content = yaml_content.replace(f"${{{key}}}", value)
                    yaml_content = yaml_content.replace(f"${key}", value)
                yaml_config = yaml.safe_load(yaml_content) or {}

        # Create settings with environment variable overrides
        return Settings(
            environment=yaml_config.get("environment", "development"),
            debug=yaml_config.get("debug", False),
            mongodb=MongoDBSettings(**yaml_config.get("mongodb", {})),
            redis=RedisSettings(**yaml_config.get("redis", {})),
            kubernetes=KubernetesSettings(**yaml_config.get("kubernetes", {})),
            docker=DockerSettings(**yaml_config.get("docker", {})),
            autoscaler=AutoscalerSettings(**yaml_config.get("autoscaler", {})),
            logging=LoggingSettings(**yaml_config.get("logging", {})),
            prometheus=PrometheusSettings(**yaml_config.get("prometheus", {})),
            grafana=GrafanaSettings(**yaml_config.get("grafana", {}))
        )


# Global settings instance
settings = Settings()