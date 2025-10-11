#!/usr/bin/env python3
"""
Local GPU testing script for systems with actual GPU hardware.
Run this on systems with GPUs to test real GPU detection.
"""

import subprocess
import sys
from plex_generate_previews.gpu_detection import detect_all_gpus, format_gpu_info


def test_gpu_detection():
    """Test real GPU detection on local hardware."""
    print("🔍 Testing GPU detection on local hardware...")
    
    try:
        gpus = detect_all_gpus()
        
        if not gpus:
            print("❌ No GPUs detected")
            assert False, "No GPUs detected"
        
        print(f"✅ Found {len(gpus)} GPU(s):")
        for i, (gpu_type, gpu_device, gpu_info) in enumerate(gpus):
            gpu_name = gpu_info.get('name', f'{gpu_type} GPU')
            gpu_desc = format_gpu_info(gpu_type, gpu_device, gpu_name)
            print(f"  [{i}] {gpu_desc}")
        
        assert len(gpus) > 0, "Should detect at least one GPU"
        
    except Exception as e:
        print(f"❌ GPU detection failed: {e}")
        assert False, f"GPU detection failed: {e}"


def test_ffmpeg_hardware_acceleration():
    """Test FFmpeg hardware acceleration capabilities."""
    print("\n🔍 Testing FFmpeg hardware acceleration...")
    
    try:
        # Test FFmpeg version
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, "FFmpeg not working properly"
        
        version_line = result.stdout.split('\n')[0]
        print(f"✅ FFmpeg version: {version_line}")
        
        # Test hardware acceleration
        result = subprocess.run(['ffmpeg', '-hwaccels'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            hwaccels = [line.strip() for line in result.stdout.split('\n') if line.strip() and not line.startswith('Hardware')]
            print(f"✅ Available hardware accelerators: {', '.join(hwaccels)}")
        else:
            print("⚠️  Could not detect hardware accelerators")
        
    except Exception as e:
        print(f"❌ FFmpeg test failed: {e}")
        assert False, f"FFmpeg test failed: {e}"


def main():
    """Run local GPU tests."""
    print("🚀 Running local GPU tests...")
    print("=" * 50)
    
    success = True
    
    # Test GPU detection
    if not test_gpu_detection():
        success = False
    
    # Test FFmpeg
    if not test_ffmpeg_hardware_acceleration():
        success = False
    
    print("\n" + "=" * 50)
    if success:
        print("✅ All local GPU tests passed!")
        sys.exit(0)
    else:
        print("❌ Some local GPU tests failed!")
        sys.exit(1)


if __name__ == '__main__':
    main()
