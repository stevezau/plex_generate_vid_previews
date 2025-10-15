FROM linuxserver/ffmpeg:8.0-cli-ls43

# Install Python, pip, gosu, and dependencies
RUN apt-get update && \
    apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip gosu pciutils && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Replace init-adduser with clean version (no branding)
COPY docker-init-user.sh /etc/s6-overlay/s6-rc.d/init-adduser/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-adduser/run

# Set working directory
WORKDIR /app

# Copy and install application
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install . --no-cache-dir

# Copy wrapper script
COPY wrapper.sh /app/wrapper.sh
RUN chmod +x /app/wrapper.sh

# Default PUID/PGID (override with environment variables)
ENV PUID=1000 \
    PGID=1000

# Tell s6-overlay to preserve environment variables
ENV S6_KEEP_ENV=1

# Use LinuxServer's /init for PUID/PGID handling
ENTRYPOINT ["/init", "/app/wrapper.sh"]

# Default: run without arguments (environment variables will be used)
# CLI arguments are supported: docker run image:tag --list-gpus --plex-url http://... --plex-token ...
CMD []
