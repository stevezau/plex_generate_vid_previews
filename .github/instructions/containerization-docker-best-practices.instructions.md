---
applyTo: '**/Dockerfile,**/Dockerfile.*,**/*.dockerfile,**/docker-compose*.yml,**/docker-compose*.yaml,**/compose*.yml,**/compose*.yaml'
description: 'Docker best practices'
---

# Docker Best Practices

## Dockerfile

- Use multi-stage builds: separate build dependencies from runtime
- Pin base image versions (never `latest` in production); prefer `-slim` or `-alpine` variants
- Order layers least→most frequently changing; copy dependency manifests before source code
- Combine `RUN` commands and clean up in the same layer (`rm -rf /var/lib/apt/lists/*`)
- Use `.dockerignore` to exclude `.git`, `node_modules`, `__pycache__`, build artifacts, `.env`
- Run as non-root user (`USER`); don't store secrets in images
- Use `COPY` over `ADD` unless extracting archives; set explicit `WORKDIR`
- Use `HEALTHCHECK` for container health monitoring

## Compose

- Pin service image versions; use named volumes for persistent data
- Set resource limits (`mem_limit`, `cpus`); define explicit networks
- Use `depends_on` with health checks for service ordering
- Externalize config via environment variables with sensible defaults

## Security

- Scan images for vulnerabilities (Trivy, Snyk)
- Never embed secrets — use env vars, Docker secrets, or mounted files
- Minimize installed packages to reduce attack surface
