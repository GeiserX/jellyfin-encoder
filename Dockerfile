# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Install necessary packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    vainfo \
    i965-va-driver \
    intel-media-va-driver-non-free \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements.txt first to leverage Docker cache
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY app/ ./

# Create source and destination directories
RUN mkdir /app/source /app/destination

# Set the entrypoint
ENTRYPOINT ["python", "monitor.py"]