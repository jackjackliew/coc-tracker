FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY coc_tracker/ ./coc_tracker/
COPY bot.py .

# Storage lives in /app by default; mount a volume to persist across restarts.
ENV PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO

# Bot runs in foreground; supervisor (Docker / systemd / k8s) handles restart.
CMD ["python", "bot.py"]
