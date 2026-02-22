# =============================================================================
# Stage 1: Builder — compile native extensions and build wheels
# =============================================================================
FROM linuxserver/ffmpeg:8.0.1-cli-ls56 AS builder

ARG SETUPTOOLS_SCM_PRETEND_VERSION=""

# Build-time only: compiler toolchain + Python + git (for setuptools-scm)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc musl-dev python3 python3-pip git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/

ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Build wheels for the app and all dependencies (pre-compiled, no gcc needed at install time)
RUN if [ -n "$SETUPTOOLS_SCM_PRETEND_VERSION" ]; then \
      SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PLEX_GENERATE_PREVIEWS=$SETUPTOOLS_SCM_PRETEND_VERSION \
      pip3 wheel --wheel-dir=/wheels --no-cache-dir .; \
    else \
      pip3 wheel --wheel-dir=/wheels --no-cache-dir .; \
    fi

# =============================================================================
# Stage 2: Runtime — lean production image (no compiler toolchain)
# =============================================================================
FROM linuxserver/ffmpeg:8.0.1-cli-ls56

# Build metadata (optional; set via --build-arg in CI)
ARG GIT_BRANCH=unknown
ARG GIT_SHA=unknown

# Runtime dependencies only — no gcc, musl-dev, or software-properties-common
# GPU drivers for hardware acceleration:
# - Intel: intel-media-va-driver-non-free (modern Gen 8+), i965-va-driver (legacy Gen 5-9)
# - AMD: mesa-va-drivers (AMD GPUs via VAAPI)
# - ARM/VideoCore: mesa-va-drivers (Mali GPUs, Raspberry Pi)
# - libva2, libva-drm2: VA-API libraries
# - vainfo: Tool to test/verify VA-API functionality
# - pciutils: Provides lspci for better GPU naming
# - git: For version detection when running from mounted git repository
# - mediainfo: For media file metadata
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    mediainfo python3 python3-pip gosu pciutils git \
    intel-media-va-driver-non-free i965-va-driver mesa-va-drivers \
    libva2 libva-drm2 vainfo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install pre-built wheels from builder (no compiler needed)
COPY --from=builder /wheels /tmp/wheels
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install --no-cache-dir --no-index /tmp/wheels/*.whl \
    --ignore-installed blinker && \
    rm -rf /tmp/wheels

# Replace init-adduser with clean version (no branding)
COPY docker-init-user.sh /etc/s6-overlay/s6-rc.d/init-adduser/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-adduser/run

# Set working directory
WORKDIR /app

# Expose build metadata to the app (non-secret)
ENV GIT_BRANCH=${GIT_BRANCH} \
    GIT_SHA=${GIT_SHA}

# Copy application source (needed for Flask templates, static files, and fixtures
# which are not yet included in package-data)
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/

# Copy wrapper script
COPY wrapper.sh /app/wrapper.sh
RUN chmod +x /app/wrapper.sh

# Default PUID/PGID (override with environment variables)
ENV PUID=1000 \
    PGID=1000

# Tell s6-overlay to preserve environment variables
ENV S6_KEEP_ENV=1

# Expose web UI port
EXPOSE 8080

# Health check for web UI — respects WEB_PORT env var
# Uses python3 stdlib instead of curl (one fewer binary dependency)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request, os; urllib.request.urlopen('http://localhost:' + os.environ.get('WEB_PORT', '8080') + '/api/health')" || exit 1

# Use LinuxServer's /init for PUID/PGID handling
ENTRYPOINT ["/init", "/app/wrapper.sh"]

# Default: run without arguments (environment variables will be used)
# CLI arguments are supported: docker run image:tag --list-gpus --plex-url http://... --plex-token ...
CMD []
