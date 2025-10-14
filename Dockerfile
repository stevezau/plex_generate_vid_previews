FROM linuxserver/ffmpeg:8.0-cli-ls43

# Install Python, pip, and gosu
RUN apt-get update && \
    apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip gosu && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create plex user/group (will be modified by entrypoint to match PUID/PGID)
RUN groupadd -g 1000 plex && \
    useradd -u 1000 -g 1000 -s /bin/bash plex

# Set working directory
WORKDIR /app

# Copy and install application
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install . --no-cache-dir

# Copy entrypoint
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Default PUID/PGID
ENV PUID=1000 \
    PGID=1000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "plex-generate-previews"]
