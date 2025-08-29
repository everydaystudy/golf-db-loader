FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY golf_loader.py ./

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Default Overpass URL env can be overridden
ENV OVERPASS_API_URL=https://overpass-api.de/api/interpreter

# Default command: process all states with safe flags unless overridden
ENTRYPOINT ["python", "golf_loader.py"]
CMD ["--skip-unchanged", "--mark-stale"]
