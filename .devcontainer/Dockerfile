# Multi-arch devcontainer for plex-generate-previews
# Supports: linux/amd64 (x86_64) and linux/arm64 (Apple Silicon, ARM servers)
# Works on: Linux, macOS (Docker Desktop), Windows (Docker Desktop / WSL2)
FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mediainfo \
    git \
    pciutils \
    vainfo \
    libva2 \
    libva-drm2 \
    mesa-va-drivers \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
