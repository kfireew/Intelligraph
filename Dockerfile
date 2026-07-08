FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl nodejs npm && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g nx@latest 2>/dev/null || true

WORKDIR /app/backend

COPY requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend/
COPY dist/ /app/dist/

RUN mkdir -p /app/backend/data/repos /app/backend/data/temp

EXPOSE 5050

ENV PYTHONUNBUFFERED=1
ENV OMP_NUM_THREADS=2
ENV OPENBLAS_NUM_THREADS=2
ENV MKL_NUM_THREADS=2
ENV NUMEXPR_NUM_THREADS=2
ENV GRAPHIFY_MAX_WORKERS=4
ENV CRG_PARSE_WORKERS=4
ENV GRAPHIFY_VIZ_NODE_LIMIT=5000
ENV INTELLIGRAPH_NETWORK_MODE=closed
# For public internet (GitHub + OpenRouter): INTELLIGRAPH_NETWORK_MODE=open
# Individual overrides: LLM_ALLOWED_HOSTS, LLM_SSL_VERIFY, INTELLIGRAPH_GIT_SSL_VERIFY
ENV LLM_SSL_VERIFY=false
ENV INTELLIGRAPH_GIT_SSL_VERIFY=false
ENV INTELLIGRAPH_ENABLE_NX_MCP=true
ENV INTELLIGRAPH_NX_COMMAND=/usr/local/bin/nx
ENV INTELLIGRAPH_NX_MCP_COMMAND=/usr/local/bin/nx

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5050/status || exit 1

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-5050}"]
