# Use the linuxserver/ffmpeg image as the base image
FROM lscr.io/linuxserver/ffmpeg:7.1.1

# Install Python and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    vainfo \
    intel-media-va-driver \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Create a virtual environment
RUN python3 -m venv venv

# Activate the virtual environment and update pip
RUN . /app/venv/bin/activate && pip install --upgrade pip

# Copy requirements.txt first to leverage Docker cache
COPY app/requirements.txt ./

# Install Python dependencies in the virtual environment
RUN . /app/venv/bin/activate && pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY app/ ./

# Ensure the source and destination directories exist
RUN mkdir -p /app/source /app/destination

# Set the entrypoint to activate the virtual environment and run the script
ENTRYPOINT ["/bin/bash", "-c", "source /app/venv/bin/activate && exec python monitor.py"]