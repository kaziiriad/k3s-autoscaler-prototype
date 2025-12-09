#!/bin/bash

# Extract certificate data from the existing kubeconfig
K3S_CA_DATA=$(grep "certificate-authority-data" ./kubeconfig | awk '{print $2}' | head -1)
K3S_CLIENT_CERT_DATA=$(grep "client-certificate-data" ./kubeconfig | awk '{print $2}')
K3S_CLIENT_KEY_DATA=$(grep "client-key-data" ./kubeconfig | awk '{print $2}')

# Set default server host if not provided
K3S_SERVER_HOST=${K3S_SERVER_HOST:-k3s-master}

# Export environment variables for substitution
export K3S_CA_DATA
export K3S_CLIENT_CERT_DATA
export K3S_CLIENT_KEY_DATA
export K3S_SERVER_HOST

# Generate the actual kubeconfig from template using envsubst
envsubst < ./kubeconfig.template > ./kubeconfig.new

# Replace the old kubeconfig
mv ./kubeconfig.new ./kubeconfig

echo "Generated kubeconfig with server host: $K3S_SERVER_HOST"