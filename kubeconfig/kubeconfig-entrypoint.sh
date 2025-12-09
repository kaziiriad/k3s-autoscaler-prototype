#!/bin/sh

# This script will be used as an entrypoint for containers that need the kubeconfig
# It substitutes environment variables in the kubeconfig before running the main command

# Check if kubeconfig needs environment substitution
if grep -q '__K3S_SERVER_HOST__' /config/kubeconfig; then
    # Substitute the K3S_SERVER_HOST environment variable
    K3S_SERVER_HOST=${K3S_SERVER_HOST:-k3s-master}
    sed -i "s/__K3S_SERVER_HOST__/$K3S_SERVER_HOST/g" /config/kubeconfig
    echo "Substituted K3S_SERVER_HOST=$K3S_SERVER_HOST in kubeconfig"
fi

# Execute the provided command
exec "$@"