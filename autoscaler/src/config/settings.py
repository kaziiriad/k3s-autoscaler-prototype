#!/usr/bin/env python3
"""
Configuration settings using Pydantic for environment variable loading
"""

import os
from typing import Optional, Dict, Any

# Try Pydantic v2 first, fallback to v1
try:
    from pydantic import Field
    from pydantic_settings import BaseSettings
except ImportError:
    # Fallback to Pydantic v1
    from pydantic.v1 import BaseSettings, Field

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, that's OK

class MongoDBSettings(BaseSettings):
    """MongoDB configuration settings"""
    url: str = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
    database_name: str = os.getenv("MONGODB_DATABASE_NAME", "autoscaler")
    connection_timeout: int = int(os.getenv("MONGODB_CONNECTION_TIMEOUT", "5"))

    class Config:
        extra = "ignore"


class RedisSettings(BaseSettings):
    """Redis configuration settings"""
    host: str = os.getenv("REDIS_HOST", "localhost")
    port: int = int(os.getenv("REDIS_PORT", "6379"))
    db: int = int(os.getenv("REDIS_DB", "0"))
    password: Optional[str] = os.getenv("REDIS_PASSWORD", None)
    connection_timeout: int = int(os.getenv("REDIS_CONNECTION_TIMEOUT", "5"))

    class Config:
        extra = "ignore"


class KubernetesSettings(BaseSettings):
    """Kubernetes configuration settings"""
    in_cluster: bool = os.getenv("KUBERNETES_IN_CLUSTER", "false").lower() == "true"
    kubeconfig_path: str = os.getenv("KUBECONFIG_PATH", "/app/kubeconfig/kubeconfig")
    server_host: Optional[str] = os.getenv("K3S_SERVER_HOST")

    class Config:
        extra = "ignore"


class DockerSettings(BaseSettings):
    """Docker configuration settings"""
    network: str = os.getenv("DOCKER_NETWORK", "prototype_k3s-network")
    worker_service: str = os.getenv("DOCKER_WORKER_SERVICE", "k3s-worker")
    image: str = os.getenv("DOCKER_IMAGE", "rancher/k3s:v1.29.1-k3s1")
    cpu_limit: str = os.getenv("DOCKER_CPU_LIMIT", "1")
    memory_limit: str = os.getenv("DOCKER_MEMORY_LIMIT", "2g")
    api_delay: int = int(os.getenv("DOCKER_API_DELAY", "3"))
    boot_time: int = int(os.getenv("DOCKER_BOOT_TIME", "30"))

    class Config:
        extra = "ignore"


class AutoscalerSettings(BaseSettings):
    """Main autoscaler configuration settings"""
    check_interval: int = int(os.getenv("AUTOSCALER_CHECK_INTERVAL", "5"))
    dry_run: bool = os.getenv("AUTOSCALER_DRY_RUN", "false").lower() == "true"

    # API settings
    api_host: str = os.getenv("AUTOSCALER_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("AUTOSCALER_API_PORT", "8080"))
    metrics_port: int = int(os.getenv("AUTOSCALER_METRICS_PORT", "9091"))

    # Threshold settings
    pending_pods_threshold: int = int(os.getenv("AUTOSCALER_PENDING_PODS_THRESHOLD", "1"))
    cpu_threshold_up: float = float(os.getenv("AUTOSCALER_CPU_THRESHOLD_UP", "80.0"))
    memory_threshold_up: float = float(os.getenv("AUTOSCALER_MEMORY_THRESHOLD_UP", "80.0"))
    cpu_threshold_down: float = float(os.getenv("AUTOSCALER_CPU_THRESHOLD_DOWN", "30.0"))
    memory_threshold_down: float = float(os.getenv("AUTOSCALER_MEMORY_THRESHOLD_DOWN", "30.0"))

    # Limit settings
    min_nodes: int = int(os.getenv("AUTOSCALER_MIN_NODES", "2"))
    max_nodes: int = int(os.getenv("AUTOSCALER_MAX_NODES", "6"))
    scale_up_cooldown: int = int(os.getenv("AUTOSCALER_SCALE_UP_COOLDOWN", "60"))
    scale_down_cooldown: int = int(os.getenv("AUTOSCALER_SCALE_DOWN_COOLDOWN", "120"))

    # Token
    k3s_token: str = os.getenv("K3S_TOKEN", "mysupersecrettoken12345")

    class Config:
        extra = "ignore"


class LoggingSettings(BaseSettings):
    """Logging configuration settings"""
    level: str = os.getenv("LOG_LEVEL", "INFO")
    format: str = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file: Optional[str] = os.getenv("LOG_FILE", None)

    class Config:
        extra = "ignore"


class PrometheusSettings(BaseSettings):
    """Prometheus configuration settings"""
    url: str = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
    query_timeout: int = int(os.getenv("PROMETHEUS_QUERY_TIMEOUT", "5"))

    class Config:
        extra = "ignore"


class GrafanaSettings(BaseSettings):
    """Grafana configuration settings"""
    admin_password: str = os.getenv("GF_SECURITY_ADMIN_PASSWORD", "admin")

    class Config:
        extra = "ignore"


class Settings(BaseSettings):
    """Main settings class that includes all sub-settings"""
    # Environment
    environment: str = os.getenv("ENVIRONMENT", "development")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Component settings
    mongodb: MongoDBSettings = Field(default_factory=MongoDBSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    docker: DockerSettings = Field(default_factory=DockerSettings)
    autoscaler: AutoscalerSettings = Field(default_factory=AutoscalerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    prometheus: PrometheusSettings = Field(default_factory=PrometheusSettings)
    grafana: GrafanaSettings = Field(default_factory=GrafanaSettings)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # Allow extra fields to avoid validation errors

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