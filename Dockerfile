# =============================================================================
# Stage 1: Builder — compile native extensions and build wheels
# =============================================================================
FROM linuxserver/ffmpeg:8.0.1-cli-ls64 AS builder

ARG SETUPTOOLS_SCM_PRETEND_VERSION=""

# Build-time only: compiler toolchain + Python + git (for setuptools-scm)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc musl-dev python3 python3-pip git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY media_preview_generator/ ./media_preview_generator/

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
ARG BUILD_DATE=unknown

# Runtime dependencies.  Split into two apt passes because we add two
# third-party repos before pulling jellyfin-ffmpeg + newer Intel drivers.
#
# GPU drivers for hardware acceleration:
# - Intel: intel-media-va-driver-non-free (modern Gen 8+), i965-va-driver (legacy Gen 5-9).
#   Ubuntu Noble ships 24.1.0 which is 18 months old and missing DG2
#   (Arc Alchemist) fixes.  We pull 25.x from Intel's graphics apt repo
#   on amd64 to get working VAAPI on Arc A380/A750/A770 + modern iGPUs.
# - AMD: mesa-va-drivers (AMD GPUs via VAAPI)
# - ARM/VideoCore: mesa-va-drivers (Mali GPUs, Raspberry Pi)
# - mesa-vulkan-drivers: Mesa's Vulkan ICDs (Intel ANV, AMD RADV, llvmpipe).
#   Used by libplacebo DV5 tone mapping on NVIDIA and AMD.  (Intel DV5 uses
#   tonemap_opencl via intel-opencl-icd instead — libplacebo+Vulkan+Intel
#   VAAPI has an upstream interop bug that returns VK_ERROR_OUT_OF_DEVICE_MEMORY
#   on both iGPU and Arc discrete inside containers.  See issue #212.)
# - intel-opencl-icd: Intel's OpenCL runtime for DV5 tone mapping via
#   tonemap_opencl (Jellyfin-ffmpeg's patched DV-aware code path).
# - libvpl2 / libmfx-gen1: Intel oneVPL runtime (QSV).
# - libva2, libva-drm2: VA-API libraries
# - vainfo: Tool to test/verify VA-API functionality
# - pciutils: Provides lspci for better GPU naming
# - git: For version detection when running from mounted git repository
# - mediainfo: For media file metadata
#
# ffmpeg: we ship jellyfin-ffmpeg (7.1.3) as /usr/lib/jellyfin-ffmpeg/ffmpeg.
# The base image also has an 8.0.1 build at /usr/local/bin/ffmpeg which we
# leave in place as a fallback (our code defaults to the Jellyfin binary
# because it carries the DV RPU-aware tonemap_opencl patches upstream lacks).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      mediainfo python3 python3-pip gosu pciutils git curl gnupg ca-certificates \
      mesa-va-drivers mesa-vulkan-drivers libva2 libva-drm2 vainfo && \
    if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      # Jellyfin apt repo (Ubuntu Noble) for jellyfin-ffmpeg7
      curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg && \
      printf 'Types: deb\nURIs: https://repo.jellyfin.org/ubuntu\nSuites: noble\nComponents: main\nArchitectures: amd64\nSigned-By: /usr/share/keyrings/jellyfin.gpg\n' \
        > /etc/apt/sources.list.d/jellyfin.sources && \
      # Intel graphics apt repo (Ubuntu Noble) for newer VAAPI + OpenCL
      curl -fsSL https://repositories.intel.com/gpu/intel-graphics.key \
        | gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg && \
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble unified" \
        > /etc/apt/sources.list.d/intel-graphics.list && \
      apt-get update && \
      apt-get install -y --no-install-recommends \
        jellyfin-ffmpeg7 \
        intel-media-va-driver-non-free \
        i965-va-driver \
        libmfx-gen1 \
        libvpl2 \
        intel-opencl-icd \
        ocl-icd-libopencl1; \
    fi && \
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

# /dev/dri/by-path/ fixup — Intel NEO (OpenCL) enumerates GPUs only via
# /dev/dri/by-path, and NVIDIA Container Toolkit populates that directory
# only for NVIDIA cards.  On mixed Intel+NVIDIA containers the Intel GPU
# is invisible to OpenCL without this (VAAPI still works).  Idempotent /
# no-op on single-vendor and bare-metal setups.  See the DV5 plan file
# for root-cause trace inside intel/compute-runtime source.
COPY docker-init-dri-by-path.sh /etc/s6-overlay/s6-rc.d/init-dri-by-path/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/init-dri-by-path/run && \
    echo oneshot > /etc/s6-overlay/s6-rc.d/init-dri-by-path/type && \
    echo /etc/s6-overlay/s6-rc.d/init-dri-by-path/run \
        > /etc/s6-overlay/s6-rc.d/init-dri-by-path/up && \
    mkdir -p /etc/s6-overlay/s6-rc.d/init-dri-by-path/dependencies.d && \
    touch /etc/s6-overlay/s6-rc.d/init-dri-by-path/dependencies.d/init-adduser && \
    touch /etc/s6-overlay/s6-rc.d/init-device-perms/dependencies.d/init-dri-by-path && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/init-dri-by-path

# Set working directory
WORKDIR /app

# Expose build metadata to the app (non-secret)
ENV GIT_BRANCH=${GIT_BRANCH} \
    GIT_SHA=${GIT_SHA} \
    BUILD_DATE=${BUILD_DATE}

# Copy application source (needed for Flask templates and static files)
COPY pyproject.toml ./
COPY media_preview_generator/ ./media_preview_generator/

# Copy wrapper script
COPY wrapper.sh /app/wrapper.sh
RUN chmod +x /app/wrapper.sh

# Persist build metadata as a JSON artifact too — startup logs read this
# alongside the env vars so a tag-drift incident is grep-able from
# `docker logs` ("Build: ..."). Falls back gracefully when build args
# aren't supplied.
RUN printf '{"branch": "%s", "sha": "%s", "built": "%s"}\n' \
        "${GIT_BRANCH}" "${GIT_SHA}" "${BUILD_DATE}" \
        > /app/build_info.json

# Default PUID/PGID (override with environment variables)
ENV PUID=1000 \
    PGID=1000 \
    ATTACHED_DEVICES_PERMS="/dev/dri -type c"

# Tell s6-overlay to preserve environment variables
ENV S6_KEEP_ENV=1

# Expose web UI port
EXPOSE 8080

# Health check for web UI — respects WEB_PORT env var
# Uses python3 stdlib instead of curl (one fewer binary dependency).
# exec replaces the shell so Docker kills the Python process on timeout (no orphans).
# timeout=5 ensures the process exits if the server is unresponsive.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD exec python3 -c "import urllib.request, os; urllib.request.urlopen('http://localhost:' + os.environ.get('WEB_PORT', '8080') + '/api/health', timeout=5)"

# Use LinuxServer's /init for PUID/PGID handling
ENTRYPOINT ["/init", "/app/wrapper.sh"]

# Web UI only; configure via the browser at http://<host>:<port>
CMD []
