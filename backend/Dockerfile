FROM python:3.12-slim

# git needed for Bitbucket clone; graph tools for building graphs
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY wheels/ wheels/
RUN pip install --no-index --find-links=wheels/ flask requests graphify code-review-graph

COPY app.py bb_auth.py mcp_server.py mcp_server_standalone.py graph_builder.py ./
COPY templates/ templates/
COPY static/ static/
RUN mkdir -p downloads /tmp/intelliscan

EXPOSE 5050

ENV PORT=5050
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT}"]