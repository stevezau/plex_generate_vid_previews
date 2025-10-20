FROM linuxserver/ffmpeg:8.0-cli-ls43

# Build metadata (optional; set via --build-arg in CI for dev images)
ARG GIT_BRANCH=unknown
ARG GIT_SHA=unknown

# Install Python, pip, gosu, and dependencies
# Install GPU drivers for hardware acceleration:
# - Intel: intel-media-va-driver-non-free (modern Gen 8+), i965-va-driver (legacy Gen 5-9)
# - AMD: mesa-va-drivers (AMD GPUs via VAAPI)
# - ARM: mesa-va-drivers (ARM Mali GPUs)
# - VideoCore: mesa-va-drivers (Raspberry Pi)
# - libva2, libva-drm2: VA-API libraries
# - vainfo: Tool to test/verify VA-API functionality
# - pciutils: Provides lspci for better GPU naming
RUN apt-get update && \
    apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip gosu pciutils \
    intel-media-va-driver-non-free i965-va-driver mesa-va-drivers libva2 libva-drm2 vainfo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Replace init-adduser with clean version (no branding)
COPY docker-init-user.sh /etc/s6-overlay/s6-rc.d/init-adduser/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-adduser/run

# Set working directory
WORKDIR /app

# Expose build metadata to the app (non-secret)
ENV GIT_BRANCH=${GIT_BRANCH} \
    GIT_SHA=${GIT_SHA}

# Copy and install application
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/
ENV PIP_BREAK_SYSTEM_PACKAGES=1
# Ensure setuptools-scm can resolve a version without VCS metadata
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PLEX_GENERATE_PREVIEWS=0.0.0.dev+${GIT_SHA}
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
