FROM python:3.12-alpine3.23 AS builder

RUN apk add --no-cache build-base cargo libffi-dev linux-headers openssl-dev

WORKDIR /build
COPY . .
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-alpine3.23

LABEL org.opencontainers.image.source="https://github.com/fantasyce/across-orchestrator"

ARG SOURCE_DATE_EPOCH=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ACROSS_ORCHESTRATOR_HOME=/var/lib/across-relay

RUN addgroup -S across-relay \
    && adduser -S -G across-relay -h /var/lib/across-relay across-relay

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* \
    && rm -rf /root/.cache /wheels

USER across-relay
EXPOSE 9444
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["across-relay", "--health"]
ENTRYPOINT ["across-relay"]
CMD ["--help"]
