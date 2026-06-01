FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in" \
    NO_PROXY="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"

COPY requirements.txt .
RUN pip install --no-cache-dir --proxy http://10.205.122.201:8080 -r requirements.txt

COPY gateway/ ./gateway/
COPY scripts/ ./scripts/

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
