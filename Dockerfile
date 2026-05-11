FROM python:3.10-slim-bullseye

# Install Blender and dependencies
RUN apt-get update && apt-get install -y \
    blender \
    libglu1-mesa \
    libxi6 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY . .

# Ensure generated directory exists
RUN mkdir -p generated

EXPOSE 5000

# Run the server
CMD ["python", "server.py"]
