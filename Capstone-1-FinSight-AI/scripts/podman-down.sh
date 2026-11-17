#!/bin/sh
set -eu

podman pod exists finsight-pod && podman pod rm -f finsight-pod || true
echo "FinSight pod stopped. Persistent data remains in the finsight-storage volume."