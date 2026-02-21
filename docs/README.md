# Documentation

> Plex Generate Previews | [Main README](../README.md)

Use this page to find the right doc quickly based on what you are trying to do.

## Choose Your Path

### I want to install and run it

- Start with [Getting Started](getting-started.md)
- Includes Docker quick start, Docker Compose, GPU setup, Unraid, and local pip install

### I want to operate it in production

- Use [Guides & Troubleshooting](guides.md)
- Includes dashboard usage, webhooks, FAQ, and troubleshooting playbooks

### I need exact settings or API behavior

- Use [Configuration & API Reference](reference.md)
- Includes configuration precedence, environment variables, CLI flags, and REST endpoints

### I want to contribute code

- Start with [Contributing & Development](../CONTRIBUTING.md)
- Includes local setup, tests, style rules, and PR workflow

## Quick Links

| Task | Go To |
|------|-------|
| Fast first run | [Getting Started](getting-started.md#quick-start-docker) |
| Configure GPU acceleration | [Getting Started — GPU](getting-started.md#gpu-acceleration) |
| Configure Unraid | [Getting Started — Unraid](getting-started.md#unraid) |
| Configure webhooks | [Guides — Webhook Integration](guides.md#webhook-integration) |
| Troubleshoot failures | [Guides — Troubleshooting](guides.md#troubleshooting) |
| Check configuration priority | [Reference — Configuration Priority](reference.md#configuration-priority) |
| Review API endpoints | [Reference — REST API](reference.md#rest-api) |

## Scope and Ownership

- [Getting Started](getting-started.md) is the source of truth for installation and initial setup
- [Guides & Troubleshooting](guides.md) is the source of truth for operations and diagnostics
- [Configuration & API Reference](reference.md) is the source of truth for settings and API contracts

## Internal vs User Documentation

The markdown files in `.github/` are maintainer and automation guidance.
They are intentionally not part of end-user product documentation.

## Architecture

### System Architecture

```mermaid
graph TB
    subgraph CLI["CLI Layer"]
        A[cli.py] --> B[config.py]
        A --> C[logging_config.py]
        A --> VC[version_check.py]
    end

    subgraph Workers["Worker Pool"]
        D[worker.py]
        D --> E1[GPU Worker 1]
        D --> E2[GPU Worker 2]
        D --> E3[CPU Worker]
    end

    subgraph Processing["Media Processing"]
        F[media_processing.py]
        G[gpu_detection.py]
        F --> H[FFmpeg]
        F --> I[BIF Generator]
    end

    subgraph External["External Services"]
        J[Plex API]
        K[plex_client.py]
    end

    subgraph Web["Web Interface"]
        W[wsgi.py / gunicorn] --> L[Flask + SocketIO]
        L --> M[auth.py]
        L --> R[routes.py]
        L --> JM[jobs.py]
        L --> N[scheduler.py]
        L --> SM[settings_manager.py]
        L --> WH[webhooks.py]
    end

    A --> D
    A --> G
    D --> F
    K --> J
    A --> K
    L --> B
    JM --> D
```

### Web Request Flow

```mermaid
sequenceDiagram
    participant Browser
    participant gunicorn
    participant Flask/SocketIO
    participant Jobs Manager
    participant Worker Pool
    participant FFmpeg
    participant Plex Config

    Browser->>gunicorn: HTTP / WebSocket
    gunicorn->>Flask/SocketIO: Route request
    Flask/SocketIO->>Jobs Manager: Create / query job
    Jobs Manager->>Worker Pool: Assign tasks
    Worker Pool->>FFmpeg: Extract frames
    FFmpeg-->>Worker Pool: JPEG frames
    Worker Pool->>Worker Pool: Generate BIF
    Worker Pool->>Plex Config: Write index-sd.bif
    Jobs Manager-->>Flask/SocketIO: Progress update
    Flask/SocketIO-->>Browser: WebSocket event
```

### Docker Container

```mermaid
graph TB
    subgraph Container["Docker Container"]
        subgraph Base["Base Image"]
            LS[linuxserver/ffmpeg]
            S6[s6-overlay init]
            PY[Python 3.12]
        end

        subgraph App["Application"]
            WEB[Web UI :8080]
            CLI_APP[CLI Mode]
            PROC[FFmpeg Processing]
        end

        S6 --> WEB
        S6 --> CLI_APP
        WEB --> PROC
        CLI_APP --> PROC
    end

    subgraph Volumes["Volume Mounts"]
        MEDIA["/media (ro)"]
        PLEX["/plex (rw)"]
        CONFIG["/config (rw)"]
    end

    MEDIA --> PROC
    PROC --> PLEX
    WEB --> CONFIG

    PORT["Port 8080"] --> WEB
```

## API Reference

Full REST API documentation is included in the [Reference](reference.md#rest-api) doc.

---

[Back to Main README](../README.md)
