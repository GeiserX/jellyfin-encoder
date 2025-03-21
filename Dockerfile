# Use an official Python runtime as a parent image
FROM python:3.13-bookworm

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive

# Install necessary packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    vainfo \
    intel-media-va-driver \
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