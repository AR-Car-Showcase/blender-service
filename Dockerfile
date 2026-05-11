FROM python:3.10-slim-bookworm

# Pin Blender version explicitly so builds are reproducible.
# Update BLENDER_VERSION when you want to move forward.
ARG BLENDER_VERSION=4.1.1
ARG BLENDER_SERIES=Blender4.1

# Install runtime deps + tools to fetch Blender binary.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    libgl1 \
    libglu1-mesa \
    libglx-mesa0 \
    libegl1 \
    libxi6 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    libxext6 \
    libxfixes3 \
    libx11-6 \
    libxxf86vm1 \
    && rm -rf /var/lib/apt/lists/*

# Install modern Blender from official release archive.
RUN curl -fsSL "https://download.blender.org/release/${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" -o /tmp/blender.tar.xz \
    && mkdir -p /opt \
    && tar -xJf /tmp/blender.tar.xz -C /opt \
    && ln -sf "/opt/blender-${BLENDER_VERSION}-linux-x64/blender" /usr/local/bin/blender \
    && rm -f /tmp/blender.tar.xz

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
