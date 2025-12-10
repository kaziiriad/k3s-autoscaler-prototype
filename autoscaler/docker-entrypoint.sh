#!/bin/bash
set -e

# Function to handle signals
cleanup() {
    echo "Received termination signal, shutting down..."
    exit 0
}

# Set up signal handlers
trap cleanup SIGTERM SIGINT

# Log the current configuration
echo "Starting K3s Autoscaler..."
echo "Environment: ${ENVIRONMENT:-development}"
echo "Debug mode: ${DEBUG:-false}"
echo "Dry run mode: ${AUTOSCALER_DRY_RUN:-false}"

# If dry run mode is enabled, we can run as non-root user
if [ "${AUTOSCALER_DRY_RUN:-false}" = "true" ]; then
    echo "Running in dry-run mode - switching to non-root user"
    exec gosu autoscaler "$@"
else
    echo "Running in production mode - need Docker socket access"
    # Check if Docker socket is accessible
    if [ ! -S /var/run/docker.sock ]; then
        echo "Warning: Docker socket not found at /var/run/docker.sock"
        echo "Scaling operations will fail without Docker socket access"
    fi

    # Check if we can access Docker
    if ! docker info >/dev/null 2>&1; then
        echo "Warning: Cannot connect to Docker daemon"
        echo "Please ensure Docker socket is mounted properly"
    fi

    exec "$@"
fi