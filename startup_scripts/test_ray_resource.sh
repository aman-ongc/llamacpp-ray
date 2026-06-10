#!/usr/bin/env bash
SSH_PASS="Ongc@1234"
RAY_HEAD_IP="10.208.211.62"
RAY_PORT="6379"
WORKER_HOST="10.208.211.54"
RAY_RESOURCE='{"text_node": 1}'

echo "=== Test 1: single-quoted resources arg via exact heredoc mechanism ==="
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$WORKER_HOST" bash -s <<REMOTE 2>&1
set -euo pipefail
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
RAY=/mnt/d/VirtualEnvironments/llm-platform/bin/ray
# Use python to validate what arg ray would receive
/mnt/d/VirtualEnvironments/llm-platform/bin/python3 -c "
import json
s = '--resources=$RAY_RESOURCE'
print('full arg:', repr(s))
val = s.split('=',1)[1]
print('value:', repr(val))
try:
    j = json.loads(val)
    print('valid JSON:', j)
except Exception as e:
    print('INVALID JSON:', e)
"
REMOTE
echo "exit: $?"

echo ""
echo "=== Test 2: what python receives with single quotes ==="
sshpass -p "$SSH_PASS" ssh -o StrictHostKeyChecking=no "$WORKER_HOST" bash -s <<REMOTE 2>&1
/mnt/d/VirtualEnvironments/llm-platform/bin/python3 -c "import sys; print('arg received:', repr(sys.argv[1]))" '--resources=$RAY_RESOURCE'
REMOTE
echo "exit: $?"
