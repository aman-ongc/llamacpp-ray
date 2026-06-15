#!/usr/bin/env bash

set -u

ENDPOINT="http://10.208.211.62:18000/v1/chat/completions"
API_KEY="sk-ongc-An0SIhEQMxAI27GLejMVqmHmFwWslrCb74SyG3LLWKw"

MAX_REQUESTS=200
REQUEST_COUNT=0
REQ_ID=1

run_request() {

```
local TOKENS=$1
local ID=$2

PROMPT=$(yes "hello" | head -n "${TOKENS}" | tr '\n' ' ')

curl \
  --noproxy '*' \
  -s \
  -o /tmp/resp_${ID}.json \
  -w "REQ=${ID} TOKENS=${TOKENS} HTTP=%{http_code} TIME=%{time_total}\n" \
  "$ENDPOINT" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d "{
    \"model\":\"ongc-llm\",
    \"messages\":[
      {
        \"role\":\"user\",
        \"content\":\"${PROMPT}\"
      }
    ],
    \"max_tokens\":100
  }"
```

}

echo
echo "===================================================="
echo "Starting text-only stress test"
echo "===================================================="

for CONCURRENCY in 1 5 10 15 20
do

```
echo
echo "####################################################"
echo "Concurrency = ${CONCURRENCY}"
echo "####################################################"

for TOKENS in 10 25 50 75 100
do

    if [ "${REQUEST_COUNT}" -ge "${MAX_REQUESTS}" ]; then
        echo
        echo "Reached limit (${MAX_REQUESTS} requests)"
        exit 0
    fi

    echo
    echo "Prompt words = ${TOKENS}"

    for ((i=1;i<=CONCURRENCY;i++))
    do

        if [ "${REQUEST_COUNT}" -ge "${MAX_REQUESTS}" ]; then
            break
        fi

        run_request "${TOKENS}" "${REQ_ID}" &

        REQUEST_COUNT=$((REQUEST_COUNT + 1))
        REQ_ID=$((REQ_ID + 1))
    done

    wait

    echo "Completed batch"
    echo "Total requests sent: ${REQUEST_COUNT}"

    sleep 2
done
```

done

echo
echo "===================================================="
echo "Stress test complete"
echo "Total requests sent: ${REQUEST_COUNT}"
echo "===================================================="
