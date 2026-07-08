FROM python:3.12-slim

# git needed for Bitbucket clone; graph tools for building graphs
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY wheels/ wheels/
# Note: semantic-router, sentence-transformers, torch wheels must be in wheels/ for closed network
# For closed network: pip download semantic-router sentence-transformers torch --dest wheels/
RUN pip install --no-index --find-links=wheels/ flask requests graphify code-review-graph cryptography semantic-router sentence-transformers numpy

COPY app.py bb_auth.py mcp_server.py mcp_server_standalone.py graph_builder.py crg_intelligence.py crg_domain_finder.py semantic_planner.py planner.py retrieval.py retriever.py merger.py resolver.py traversal.py ranker.py file_fetcher.py tune.py ./
COPY models/ models/
COPY templates/ templates/
COPY static/ static/
RUN mkdir -p downloads /tmp/intelliscan

EXPOSE 5050

ENV PORT=5050
ENV PYTHONUNBUFFERED=1
ENV INTELLIGRAPH_NETWORK_MODE=closed
ENV INTELLIGRAPH_REQUIRE_SSO=true
# SECRET_KEY must be set at runtime (min 32 chars) when REQUIRE_SSO=true
# For public internet (GitHub + OpenRouter), set:
#   INTELLIGRAPH_NETWORK_MODE=open

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT}"]