FROM python:3.12-slim AS builder

WORKDIR /build
COPY retrieval/requirements.txt .
RUN pip install --no-cache-dir --target=/deps -r requirements.txt

FROM python:3.12-slim

RUN groupadd -r agent && useradd --no-log-init -r -g agent agent
WORKDIR /app

COPY --from=builder /deps /usr/local/lib/python3.12/site-packages
COPY retrieval/ retrieval/

USER agent
EXPOSE 8080

# Build context must be app/ (docker build -f retrieval/Dockerfile .)
CMD ["python", "-m", "uvicorn", "retrieval.main:app", "--host", "0.0.0.0", "--port", "8080"]
