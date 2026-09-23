# Playwright's image ships Chromium and its system libraries (amd64 + arm64)
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONUNBUFFERED=1 \
    DB_PATH=/data/carwatch.db DEBUG_DIR=/data/debug PORT=8080
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8080
CMD ["python3", "-m", "app.server"]
