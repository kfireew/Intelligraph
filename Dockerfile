FROM intelligraph-optimised:latest

WORKDIR /app/backend

COPY backend/ /app/backend/
COPY dist/ /app/dist/

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
ENV INTELLIGRAPH_REQUIRE_SSO=true
# SECRET_KEY must be set at runtime (min 32 chars) when REQUIRE_SSO=true

VOLUME /app/backend/data

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5050/status || exit 1

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-5050}"]
