# GPU Detection Skill

Expertise in GPU hardware detection and FFmpeg hardware acceleration configuration for video processing.

## When to Use

- Debugging GPU detection failures
- Adding support for new GPU types
- Troubleshooting codec/driver issues
- Optimizing hardware acceleration

## GPU Detection Flow

```
detect_all_gpus() → [detect_nvidia(), detect_intel(), detect_amd(), detect_apple()]
                  → filter available GPUs
                  → return GPUInfo list with hwaccel config
```

## Supported Hardware

| Vendor | Detection Method | FFmpeg hwaccel | Decoder |
|--------|------------------|----------------|---------|
| NVIDIA | `pynvml` / `nvidia-smi` | `cuda` | `_cuvid` suffix |
| Intel | `/dev/dri/renderD*` | `qsv` | `_qsv` suffix |
| AMD | `amdsmi` / `/dev/dri` | `vaapi` | vaapi decode |
| Apple | `sysctl hw.model` | `videotoolbox` | native |

## Key Files

- [gpu_detection.py](../../../plex_generate_previews/gpu_detection.py) - All detection logic
- [media_processing.py](../../../plex_generate_previews/media_processing.py) - FFmpeg command building

## Common Issues

**NVIDIA not detected**: Check `pynvml` installed, NVIDIA driver loaded, container has `--gpus all`

**Intel QSV fails**: Verify `/dev/dri/renderD128` accessible, `intel-media-va-driver` installed

**Codec not supported**: GPU may not support codec (e.g., AV1 on older NVIDIA). Falls back to CPU.

## Testing GPU Code

```bash
# Skip GPU tests in CI (no hardware)
pytest -m "not gpu"

# Run GPU tests locally
pytest -m gpu -v
```

## Adding New GPU Support

1. Add detection function: `def detect_<vendor>() -> list[GPUInfo]`
2. Add to `detect_all_gpus()` dispatcher
3. Define hwaccel/decoder configuration
4. Add tests with mocked hardware responses
