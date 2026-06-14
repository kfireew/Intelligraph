FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY backend/ /app/backend/
COPY dist/ /app/dist/
COPY requirements.txt /app/backend/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/backend/data/repos /app/backend/data/temp

EXPOSE 5050
ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "python app.py --host 0.0.0.0 --port ${PORT:-5050}"]