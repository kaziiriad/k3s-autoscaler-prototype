#!/bin/sh
echo "Waiting for kubeconfig to be generated..."
while [ ! -f /output/kubeconfig ]; do
  sleep 2
done

echo "Kubeconfig found, fixing server address..."
sed -i 's|https://127.0.0.1:6443|https://k3s-master:6443|g' /output/kubeconfig
echo "Kubeconfig fixed! Server address updated to https://k3s-master:6443"

# Keep monitoring and fixing in case it gets regenerated
while true; do
  sleep 10
  sed -i 's|https://127.0.0.1:6443|https://k3s-master:6443|g' /output/kubeconfig
done