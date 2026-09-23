# Playwright's image ships Chromium and its system libraries (amd64 + arm64)
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 \
    DB_PATH=/data/carwatch.db DEBUG_DIR=/data/debug PORT=8080
WORKDIR /srv
COPY requirements.txt .
RUN echo "Using package index: $PIP_INDEX_URL" && \
    pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt
COPY app ./app
EXPOSE 8080
CMD ["python3", "-m", "app.server"]
