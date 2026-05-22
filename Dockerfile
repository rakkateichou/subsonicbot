FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATABASE_PATH=/app/data/users.db

# Set work directory
WORKDIR /app

# Install system dependencies (like curl for debugging/healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Pre-create data directory for persistent SQLite volume mounts
RUN mkdir -p /app/data

# Copy pyproject.toml and install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy the rest of the project files
COPY . .

# Expose the web server port
EXPOSE 8080

# Command to run the Flask / Telebot app
CMD ["python", "main.py"]
