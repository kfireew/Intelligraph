FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git curl && \
    rm -rf /var/lib/apt/lists/*

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
ENV LLM_ALLOWED_HOSTS=models.al-services.idf.cts

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5050/status || exit 1

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-5050}"]
