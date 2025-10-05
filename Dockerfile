# Simple Dockerfile to run the uesave webapp on Ubuntu
FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000

# System deps: Python + venv + certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtualenv; upgrade pip tooling
RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

# App directory
WORKDIR /app

# Copy only project files; running from source so templates/static are available
COPY uesave/ uesave/
COPY pyproject.toml README.md ./

# Install runtime dependencies from pyproject
# We intentionally install just the runtime deps so we can run from source
RUN pip install --no-cache-dir \
    "fastapi>=0.100" \
    "uvicorn[standard]>=0.22" \
    "jinja2>=3.1" \
    "python-multipart>=0.0.5"

# Optional: run as non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Start uvicorn; allow HOST/PORT overrides via env
CMD ["sh", "-c", "uvicorn uesave.webapp:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
