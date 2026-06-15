#!/usr/bin/env bash

set -u

ENDPOINT="http://10.208.211.62:18000/v1/chat/completions"
API_KEY="sk-ongc-An0SIhEQMxAI27GLejMVqmHmFwWslrCb74SyG3LLWKw"

for WORDS in $(seq 2 5000 65000)
do
echo
echo "===================================================="
echo "Testing prompt with ${WORDS} words"
echo "===================================================="

```
TMP_PROMPT=$(mktemp)
TMP_REQ=$(mktemp)
TMP_RESP=$(mktemp)

yes "hello" | head -n "${WORDS}" | tr '\n' ' ' > "${TMP_PROMPT}"

printf '{"model":"ongc-llm","messages":[{"role":"user","content":"' > "${TMP_REQ}"
cat "${TMP_PROMPT}" >> "${TMP_REQ}"
printf '"}],"max_tokens":100}' >> "${TMP_REQ}"

START=$(date +%s)

HTTP_CODE=$(curl \
    --noproxy '*' \
    -s \
    -o "${TMP_RESP}" \
    -w "%{http_code}" \
    "${ENDPOINT}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${API_KEY}" \
    --data-binary @"${TMP_REQ}")

END=$(date +%s)

echo "HTTP Code      : ${HTTP_CODE}"
echo "Elapsed Seconds: $((END-START))"

if [ -f "${TMP_RESP}" ]; then

    PROMPT_TOKENS=$(jq -r '.usage.prompt_tokens // "N/A"' "${TMP_RESP}" 2>/dev/null)
    TOTAL_TOKENS=$(jq -r '.usage.total_tokens // "N/A"' "${TMP_RESP}" 2>/dev/null)
    NODE_IP=$(jq -r '.node_ip // "N/A"' "${TMP_RESP}" 2>/dev/null)

    echo "Prompt Tokens  : ${PROMPT_TOKENS}"
    echo "Total Tokens   : ${TOTAL_TOKENS}"
    echo "Node           : ${NODE_IP}"
fi

if [ "${HTTP_CODE}" != "200" ]; then
    echo
    echo "FAILED RESPONSE:"
    cat "${TMP_RESP}"
    break
fi

rm -f "${TMP_PROMPT}" "${TMP_REQ}" "${TMP_RESP}"

sleep 2
```

done
