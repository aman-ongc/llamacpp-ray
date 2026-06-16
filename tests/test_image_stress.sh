#!/usr/bin/env bash

set -u

ENDPOINT="http://10.208.211.62:18000/v1/chat/completions"
API_KEY="sk-ongc-An0SIhEQMxAI27GLejMVqmHmFwWslrCb74SyG3LLWKw"

RESULTS_FILE="stress_results.csv"

TOTAL_REQUESTS=300
REQUESTS_PER_MINUTE=60
TEST_MINUTES=5

echo "req_id,http_code,latency_sec" > "${RESULTS_FILE}"

send_request() {

```
local REQ_ID=$1

WORDS=$((10 + RANDOM % 91))

PROMPT=$(yes "hello" | head -n "${WORDS}" | tr '\n' ' ')

RESULT=$(curl \
    --noproxy '*' \
    -s \
    -o /tmp/resp_${REQ_ID}.json \
    -w "%{http_code},%{time_total}" \
    "${ENDPOINT}" \
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
    }")

HTTP_CODE=$(echo "${RESULT}" | cut -d',' -f1)
LATENCY=$(echo "${RESULT}" | cut -d',' -f2)

echo "${REQ_ID},${HTTP_CODE},${LATENCY}" >> "${RESULTS_FILE}"

echo "$(date '+%H:%M:%S') REQ=${REQ_ID} HTTP=${HTTP_CODE} LAT=${LATENCY}s"
```

}

REQ_ID=1

echo
echo "===================================================="
echo "Starting rate-aware stress test"
echo "Rate limit      : ${REQUESTS_PER_MINUTE}/minute"
echo "Duration        : ${TEST_MINUTES} minutes"
echo "Total requests  : ${TOTAL_REQUESTS}"
echo "===================================================="
echo

for MINUTE in $(seq 1 ${TEST_MINUTES})
do

```
echo
echo "================ Minute ${MINUTE}/${TEST_MINUTES} ================"

MINUTE_START=$(date +%s)

for SLOT in $(seq 1 12)
do

    echo "Batch ${SLOT}/12"

    for i in $(seq 1 5)
    do
        send_request "${REQ_ID}" &
        REQ_ID=$((REQ_ID + 1))
    done

    wait

    sleep 5
done

MINUTE_END=$(date +%s)
ELAPSED=$((MINUTE_END - MINUTE_START))

echo "Minute runtime: ${ELAPSED}s"
```

done

echo
echo "===================================================="
echo "Test completed"
echo "===================================================="

TOTAL=$(tail -n +2 "${RESULTS_FILE}" | wc -l)

SUCCESS=$(awk -F',' '$2=="200"{count++} END{print count+0}' "${RESULTS_FILE}")
FAILED=$(awk -F',' '$2!="200"{count++} END{print count+0}' "${RESULTS_FILE}")

echo "Total Requests : ${TOTAL}"
echo "Success        : ${SUCCESS}"
echo "Failed         : ${FAILED}"

awk -F',' 'NR>1 {print $3}' "${RESULTS_FILE}" | sort -n > /tmp/latencies.txt

AVG=$(awk '{sum+=$1} END {printf "%.3f", sum/NR}' /tmp/latencies.txt)

COUNT=$(wc -l < /tmp/latencies.txt)

P50=$(awk -v n="${COUNT}" 'NR==int(n*0.50){print}' /tmp/latencies.txt)
P95=$(awk -v n="${COUNT}" 'NR==int(n*0.95){print}' /tmp/latencies.txt)
P99=$(awk -v n="${COUNT}" 'NR==int(n*0.99){print}' /tmp/latencies.txt)

echo
echo "Latency Statistics"
echo "Average : ${AVG}s"
echo "P50     : ${P50}s"
echo "P95     : ${P95}s"
echo "P99     : ${P99}s"

echo
echo "Results saved to:"
echo "${RESULTS_FILE}"
