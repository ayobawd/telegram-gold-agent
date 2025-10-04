# Use official Python image
FROM python:3.9-slim

# Install dependencies
RUN pip install fastapi uvicorn requests

# Copy source code
COPY . /app
WORKDIR /app

# Expose port (default 3000), OnDemand uses PORT environment variable
ENV PORT=3000

# Start the FastAPI server
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-3000}"]
