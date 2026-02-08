# Documentation

> Plex Generate Previews | [Main README](../README.md)

## Quick Links

| Doc | Description |
|-----|-------------|
| [Quick Start & Docker](quickstart.md) | Get running in 5 minutes, Docker Compose, networking |
| [Configuration](configuration.md) | All options + path mappings |
| [GPU Support](gpu-support.md) | NVIDIA, AMD, Intel acceleration |
| [Web Interface](web-interface.md) | Dashboard and scheduling |
| [FAQ](faq.md) | Questions + troubleshooting |

## Platform Guides

| Platform | Guide |
|----------|-------|
| Docker/Compose | [Quick Start & Docker](quickstart.md) |
| Unraid | [Unraid Guide](unraid.md) |

## Architecture

```mermaid
graph TB
    subgraph CLI["CLI Layer"]
        A[cli.py] --> B[config.py]
        A --> C[logging_config.py]
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
        L[Flask + SocketIO]
        M[auth.py]
        N[scheduler.py]
    end
    
    A --> D
    A --> G
    D --> F
    K --> J
    A --> K
    L --> A
```

## API Reference

| Doc | Description |
|-----|-------------|
| [API Reference](API.md) | REST API documentation |

---

[Back to Main README](../README.md)
