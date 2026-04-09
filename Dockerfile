FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY *.py ./

# Use start.py which upgrades yt-dlp on every start
CMD ["python", "start.py"]
