# Use official Python image (>=3.10 supports PEP 604 unions)
FROM python:3.11-slim

# Safety & cleaner logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn requests

# Copy source code
WORKDIR /app
COPY . /app

# Expose port for local runs (platforms usually ignore this)
EXPOSE 3000

# Start the FastAPI server
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-3000} --proxy-headers"]
