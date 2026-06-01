#!/usr/bin/env bash
set -euo pipefail

export http_proxy="${http_proxy:-http://10.205.122.201:8080}"
export https_proxy="${https_proxy:-http://10.205.122.201:8080}"
export no_proxy="${no_proxy:-localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in}"
export NO_PROXY="${NO_PROXY:-$no_proxy}"
export RAY_grpc_enable_http_proxy=false

RAY_HEAD_IP="${RAY_HEAD_IP:-10.208.211.62}"
RAY_PORT="${RAY_PORT:-6379}"
RAY_DASHBOARD_PORT="${RAY_DASHBOARD_PORT:-8265}"

ray start --address="${RAY_HEAD_IP}:${RAY_PORT}" --num-gpus=1 --num-cpus=6 --block
