#!/usr/bin/with-contenv bash
# shellcheck shell=bash
#
# Populate /dev/dri/by-path/ with pci-<addr>-render / pci-<addr>-card
# symlinks for every DRM device exposed in the container.
#
# Why: Intel NEO (OpenCL) enumerates GPUs by scanning /dev/dri/by-path/
# and reading /sys/class/drm/renderD*/device — there is NO fallback to
# /dev/dri/renderD*.  NVIDIA Container Toolkit populates by-path only
# for the NVIDIA cards it manages, so in mixed Intel+NVIDIA containers
# under --runtime=nvidia the Intel iGPU/dGPU is invisible to OpenCL
# even though VAAPI works fine on /dev/dri/renderD*.  Bare metal + udev
# populates everything so single-vendor hosts already work; this script
# is a no-op in that case.
#
# Safe on every GPU configuration (idempotent ln -sf; existing symlinks
# are skipped).  See plan file dapper-plotting-meadow.md for full
# rationale and upstream references.
set -euo pipefail

[ -d /dev/dri ] || exit 0

mkdir -p /dev/dri/by-path

populate() {
    local suffix="$1"    # render or card
    local glob="$2"      # renderD* or card*
    local node name dev_link pci link
    for node in /dev/dri/$glob; do
        [ -e "$node" ] || continue
        name=$(basename "$node")
        # Skip card connector nodes (cardN-HDMI-A-1 etc.)
        [[ "$suffix" == "card" && "$name" == *-* ]] && continue
        dev_link=$(readlink -f "/sys/class/drm/$name/device" 2>/dev/null || true)
        [ -n "$dev_link" ] || continue
        pci=$(basename "$dev_link")
        # Sanity: pci should look like XXXX:XX:XX.X
        [[ "$pci" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$ ]] || continue
        link="/dev/dri/by-path/pci-${pci}-${suffix}"
        if [ -e "$link" ] || [ -L "$link" ]; then
            continue
        fi
        ln -sf "../$name" "$link"
        echo "**** added /dev/dri/by-path/pci-${pci}-${suffix} -> ../$name ****"
    done
}

populate render 'renderD*'
populate card   'card*'

exit 0
