FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user (for security when not using Docker socket)
RUN groupadd -r autoscaler && useradd -r -g autoscaler autoscaler

# Install Python dependencies
# Consolidate all requirements into one file
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code with proper ownership
COPY --chown=autoscaler:autoscaler src/ ./src/
COPY --chown=autoscaler:autoscaler config/ ./config/
COPY --chown=autoscaler:autoscaler .env* ./

# Create necessary directories
RUN mkdir -p /app/logs /app/data && \
    chown -R autoscaler:autoscaler /app/logs /app/data

# Expose ports
EXPOSE 8080 9091

# Health check with improved timeout and retries
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Use root user for Docker socket access (needed for scaling)
# In production, consider using Docker-in-Docker or Docker socket proxy
USER root

# Add script to handle dry-run mode
COPY --chown=autoscaler:autoscaler docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set the entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]

# Default command
CMD ["python", "src/main.py"]