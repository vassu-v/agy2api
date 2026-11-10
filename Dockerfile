# Multi-stage build for agy2api
FROM python:3.13-slim as base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# Install tzdata, jemalloc and set timezone
# jemalloc replaces glibc malloc, drastically reducing memory fragmentation
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata libjemalloc2 && \
    ln -sf /usr/share/zoneinfo/UTC /etc/localtime && \
    echo "UTC" > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Use jemalloc instead of glibc malloc
# jemalloc actively returns idle memory to the OS, preventing RSS memory growth
ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
# jemalloc config: enable background thread reclamation, 5s dirty decay
ENV MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000"

WORKDIR /app

# Copy only requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 7861

# Start via web.py, enabling HOST/PORT environment variable configuration
CMD ["python", "web.py"]
