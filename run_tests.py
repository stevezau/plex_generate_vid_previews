#!/usr/bin/env python3
"""
Test runner for the test suite.
"""

import subprocess
import sys


def main():
    """Run the test suite."""
    print("=" * 60)
    print("🧪 Plex Generate Previews Test Runner")
    print("=" * 60)
    print("🚀 Running test suite...")
    
    cmd = [
        'pytest',
        'tests/',
        '-v',
        '--tb=short',
        '--cov=plex_generate_previews',
        '--cov-report=term-missing'
    ]
    
    result = subprocess.run(cmd)
    
    print("\n" + "=" * 60)
    if result.returncode == 0:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed!")
    
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
