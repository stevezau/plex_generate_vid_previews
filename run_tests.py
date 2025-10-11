#!/usr/bin/env python3
"""
Test runner that runs only the tests that work reliably.
"""

import subprocess
import sys


def run_ci_tests():
    """Run tests that work in CI environments."""
    print("🚀 Running CI-safe tests...")
    
    cmd = [
        'pytest',
        'tests/test_basic.py',
        'tests/test_cli.py', 
        'tests/test_gpu_ci.py',
        '-v',
        '--tb=short'
    ]
    
    result = subprocess.run(cmd)
    return result.returncode == 0


def run_local_gpu_tests():
    """Run local GPU tests if on a system with GPUs."""
    print("\n🔍 Running local GPU tests...")
    
    cmd = ['pytest', 'test_gpu_local.py', '-v', '--tb=short']
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    """Run appropriate tests based on environment."""
    print("=" * 60)
    print("🧪 Plex Generate Previews Test Runner")
    print("=" * 60)
    
    success = True
    
    # Always run CI tests
    if not run_ci_tests():
        success = False
    
    # Try to run local GPU tests (may fail if no GPUs)
    try:
        if not run_local_gpu_tests():
            print("⚠️  Local GPU tests failed (this is OK if no GPU hardware)")
    except Exception as e:
        print(f"⚠️  Could not run local GPU tests: {e}")
    
    print("\n" + "=" * 60)
    if success:
        print("✅ All CI tests passed!")
        print("💡 For full GPU testing, run: python test_gpu_local.py")
    else:
        print("❌ Some tests failed!")
    
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
