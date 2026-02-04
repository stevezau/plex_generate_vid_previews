# Frequently Asked Questions

> [Back to Docs](README.md)

---

## General

### What does this tool do?

Generates video preview thumbnails (BIF files) for Plex Media Server. These are the small images you see when scrubbing through videos. Plex's built-in generation is slow - this tool makes it 5-10x faster using GPU acceleration.

### Does this work on Windows?

Yes! Windows supports GPU acceleration via D3D11VA, which works with NVIDIA, AMD, and Intel GPUs. Install the latest GPU drivers and it just works.

### Can I use this without a GPU?

Yes! Set `--gpu-threads 0` and `--cpu-threads 4` (or higher) for CPU-only processing.

### What's the difference between web mode and CLI mode?

- **Web mode** (default): Runs a dashboard at port 8080 for managing jobs and schedules
- **CLI mode** (`--cli`): Runs one-time processing and exits

---

## GPUs

### How do I know which GPUs are detected?

```bash
plex-generate-previews --list-gpus
```

### Can I use multiple GPUs?

Yes! The tool automatically detects and can use multiple GPUs. Use `--gpu-selection "0,1,2"` to select specific ones.

### Which GPU should I use?

| GPU Type | Best For |
|----------|----------|
| NVIDIA | Fastest for video processing |
| Intel iGPU | Great for low-power setups, common on Unraid |
| AMD | Good VAAPI support on Linux |
| CPU-only | Works everywhere, slower |

---

## Performance

### How many threads should I use?

| Scenario | GPU Threads | CPU Threads |
|----------|-------------|-------------|
| Balanced | 4 | 4 |
| High-end | 8 | 4 |
| CPU-only | 0 | 8 |

### What's thumbnail quality 1-10?

Lower numbers = higher quality but larger file sizes.
- Quality 2 = Highest quality
- Quality 4 = Default (good balance)
- Quality 10 = Lowest quality

---

## Docker

### Why does my container fail to start?

Most common cause: Using `init: true` in docker-compose. Remove it - this container uses s6-overlay.

### Why can't the container find my files?

Path mapping issue. See [Path Mappings Guide](path-mappings.md).

### How do I get the auth token?

```bash
docker logs plex-generate-previews | grep "Token:"
```

---

## Processing

### Can I process specific libraries only?

Yes! Use `--plex-libraries "Movies, TV Shows"` to process only specific libraries.

### How do I regenerate existing thumbnails?

Use `--regenerate-thumbnails` or set `REGENERATE_THUMBNAILS=true`.

### Why is it "skipping" some files?

Possible causes:
- Thumbnails already exist (use `--regenerate-thumbnails` to force)
- File not found (check path mappings)
- Invalid file format

---

## Troubleshooting

### "Skipping as file not found"

Path mapping issue. See [Path Mappings Guide](path-mappings.md).

### "GPU permission denied"

Set PUID/PGID environment variables to match your user.

### How do I enable debug logging?

```bash
--log-level DEBUG
# or
-e LOG_LEVEL=DEBUG
```

---

## More Questions?

Open a [GitHub Issue](https://github.com/stevezau/plex_generate_vid_previews/issues)

---

## Troubleshooting

### Container Issues

**"s6-overlay-suexec: fatal: can only run as pid 1"**

Remove `init: true` from docker-compose or `--init` flag. This container uses s6-overlay as its init system.

---

### GPU Issues

**"No GPUs detected"**

1. Pass through GPU device:
   ```bash
   # Intel/AMD
   --device /dev/dri:/dev/dri
   # NVIDIA
   --gpus all
   ```
2. Update FFmpeg to 7.0+
3. Install GPU drivers on host
4. Verify: `plex-generate-previews --list-gpus`

**"GPU permission denied" (Intel/AMD)**
```bash
-e PUID=1000 -e PGID=1000
# or
--group-add video
```

---

### File Access Issues

**"Skipping as file not found"**

Path mapping issue. See [Configuration - Path Mappings](configuration.md#path-mappings).

**"Permission denied"**
```bash
id  # Get your UID/GID
-e PUID=1000 -e PGID=1000
```

**"PLEX_CONFIG_FOLDER does not exist"**

Verify path contains Plex structure:
```bash
ls -la "/path/to/Library/Application Support/Plex Media Server"
# Should contain: Cache, Media, Metadata folders
```

---

### Connection Issues

**"Connection failed to Plex"**

1. Don't use `localhost` in Docker, use actual IP
2. Verify URL format: `http://192.168.1.100:32400`
3. Check Plex is running
4. Test: `curl -H "X-Plex-Token: TOKEN" http://IP:32400/status/sessions`

---

### Debug Mode

Enable detailed logging:
```bash
-e LOG_LEVEL=DEBUG
# or
--log-level DEBUG
```

---

[Back to Docs](README.md) | [Main README](../README.md)
