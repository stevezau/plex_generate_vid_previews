# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Setup Wizard**: New first-time setup wizard with 4-step guided configuration
- **Plex OAuth**: Sign in with Plex account via OAuth (no manual token copying)
- **Settings Page**: Web-based settings management with live save
- **Settings Persistence**: Configuration saved to `/config/settings.json`
- **Connection Status**: Dashboard shows Plex connection status and server info
- **Library Selection**: Select which libraries to process via web UI
- **Path Configuration**: Configure path mappings through the web interface
- **Worker Status Cards**: Real-time display of GPU and CPU worker status
- **Job Logs Viewer**: View job logs directly in the dashboard
- **Browser Notifications**: Get notified when jobs complete or fail
- **Multi-Library Selection**: Select multiple libraries for a job (not just one or all)
- **Networking Documentation**: New guide for Docker network configuration
- **App Icon**: Custom icon for Unraid and web UI favicon

### Changed
- `PLEX_URL` and `PLEX_TOKEN` environment variables are now optional (configured via UI)
- Configuration priority: CLI args > settings.json > env vars > defaults
- Improved documentation with focus on web-based setup
- Dashboard polls jobs every 5 seconds for real-time updates
- Unraid template updated with networking guidance and icon

### Fixed
- Settings manager singleton properly reinitializes with config_dir
- Fixed `working_tmp_folder` not initialized when running jobs from web UI
- Fixed library filtering - now correctly passes library names (not IDs)
- Fixed Plex OAuth preferring local connections over plex.direct URLs
- Fixed path validation in setup wizard

---

## [1.0.0] - Previous Release

### Features
- GPU-accelerated BIF file generation (NVIDIA, AMD, Intel, macOS)
- Parallel processing with configurable GPU and CPU workers
- Web dashboard with job management
- Real-time progress updates via WebSocket
- Job scheduling (cron and interval-based)
- Docker image with GPU support
- Unraid Community Applications template

### GPU Support
- NVIDIA: CUDA/NVENC
- Intel: VAAPI/QuickSync
- AMD: VAAPI
- macOS: VideoToolbox
- Windows: D3D11VA

---

## Template for Future Releases

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added
- New features

### Changed
- Changes in existing functionality

### Deprecated
- Soon-to-be removed features

### Removed
- Removed features

### Fixed
- Bug fixes

### Security
- Vulnerability fixes
```
