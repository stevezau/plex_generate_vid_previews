FROM linuxserver/ffmpeg:8.0-cli-ls43

# Install Python and pip, then clean up
RUN apt-get update && \
    apt-get install -y mediainfo software-properties-common gcc musl-dev python3 python3-pip && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r plex && useradd -r -g plex plex

# Set the working directory in the container
WORKDIR /app

# Copy only necessary files
COPY pyproject.toml ./
COPY plex_generate_previews/ ./plex_generate_previews/

# Install Python package
ENV PIP_BREAK_SYSTEM_PACKAGES 1
RUN pip3 install . --no-cache-dir

# Change ownership to non-root user
RUN chown -R plex:plex /app

# Switch to non-root user
USER plex

ENTRYPOINT ["plex-generate-previews"]
