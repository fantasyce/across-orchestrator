FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/fantasyce/across-orchestrator"

ARG SOURCE_DATE_EPOCH=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ACROSS_ORCHESTRATOR_HOME=/var/lib/across-relay

RUN addgroup --system across-relay \
    && adduser --system --ingroup across-relay --home /var/lib/across-relay across-relay

WORKDIR /opt/across-orchestrator
COPY . .
RUN python -m pip install --no-cache-dir . \
    && rm -rf /root/.cache /opt/across-orchestrator/.git

USER across-relay
EXPOSE 9444
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["across-relay", "--health"]
ENTRYPOINT ["across-relay"]
CMD ["--help"]
