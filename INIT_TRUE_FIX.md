# Fix for `init: true` Issue

## The Problem
Users adding `init: true` to docker-compose.yml get this error:
```
s6-overlay-suexec: fatal: can only run as pid 1
```

## Why It Happens
- This container uses **s6-overlay** (LinuxServer.io base) as its init system
- Docker's `init: true` adds **tini** as PID 1
- Only ONE process can be PID 1
- s6-overlay detects it's not PID 1 and refuses to run

## Why `init: true` Should Be Removed
s6-overlay is MORE capable than Docker's basic init:

| Feature | Docker init (tini) | s6-overlay |
|---------|-------------------|------------|
| Reap zombies | ✅ | ✅ |
| Signal forwarding | ✅ | ✅ |
| Process supervision | ❌ | ✅ |
| Auto-restart | ❌ | ✅ |
| PUID/PGID support | ❌ | ✅ |
| Init scripts | ❌ | ✅ |

**Adding `init: true` = Losing features for no benefit**

## The Solution (Simple!)

### What We Did:
1. ✅ Added detection in `wrapper.sh` - catches the issue early
2. ✅ Shows clear, helpful error message explaining:
   - What's wrong
   - What they lose
   - How to fix it
3. ✅ Created `docker-compose.example.yml` - working example
4. ✅ Updated README with warnings and troubleshooting

### What the User Sees:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
❌ ERROR: 'init: true' detected in your Docker configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This container uses s6-overlay which is MORE capable than Docker's
basic init. Using 'init: true' prevents s6-overlay from running and
you LOSE these features:

  ❌ PUID/PGID support (file permissions will be wrong!)
  ❌ Process supervision and auto-restart
  ❌ Proper initialization scripts
  ❌ Better signal handling and logging

HOW TO FIX:

  Remove the 'init: true' line from your docker-compose.yml

Why? s6-overlay is already a better init system - you don't need both!
```

### What They Do:
1. Remove `init: true` from docker-compose.yml
2. Restart: `docker-compose down && docker-compose up -d`
3. Done! ✅

## Files Changed

### Modified:
- **wrapper.sh** - Added init detection and helpful error (12 lines added)
- **README.md** - Added warning and troubleshooting section
- **docker-compose.example.yml** - Created working example with comments

### Not Changed:
- **Dockerfile** - Stays simple (no fallback logic)
- **Application code** - No changes needed

## Result
✅ **Simple, elegant solution**
✅ **Clear error message guides users**
✅ **No complex fallback logic**
✅ **Container stays clean and maintainable**
✅ **Users understand WHY they need to remove it**

## For the User in the Screenshot

Tell them to:
1. Edit their docker-compose.yml
2. Remove this line:
   ```yaml
   init: true  # ← DELETE THIS
   ```
3. Run: `docker-compose down && docker-compose up -d`

That's it! The container will start normally.

