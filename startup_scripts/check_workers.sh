#!/usr/bin/env bash
SSH_PASS="Ongc@1234"
_ssh() {
  local host="$1"; shift
  sshpass -p "$SSH_PASS" ssh -n -o StrictHostKeyChecking=no -o ConnectTimeout=5 "administrator@$host" "$@"
}

for node in 10.208.211.52 10.208.211.53 10.208.211.54 10.208.211.55 10.208.211.56 10.208.211.57 10.208.211.58 10.208.211.59 10.208.211.60 10.208.211.61 10.208.211.63 10.208.211.64 10.208.211.65 10.208.211.67; do
  echo "=== $node ==="
  echo -n "  raylet:       "; _ssh "$node" "pgrep raylet && echo RUNNING || echo NOT_RUNNING" 2>&1
  echo -n "  llama-server: "; _ssh "$node" "pgrep llama-server && echo RUNNING || echo NOT_RUNNING" 2>&1
  echo -n "  llama health: "; _ssh "$node" "curl --noproxy '*' -sf --max-time 3 http://\$(hostname -I | awk '{print \$1}'):8080/health && echo OK || echo FAIL" 2>&1
  echo "  --- ray-worker.log (last 10) ---"
  _ssh "$node" "tail -10 /tmp/ray-worker.log 2>/dev/null || echo NO_LOG" 2>&1
  echo "  --- llama-server.log (last 8) ---"
  _ssh "$node" "tail -8 /tmp/llama-server.log 2>/dev/null || echo NO_LOG" 2>&1
done
