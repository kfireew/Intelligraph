#!/usr/bin/env python3
"""Start backend at D:\6.2.26\Kfirs-Intelligraph\backend for parity with copy."""
import subprocess
import sys
import os

def main():
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, "app.py", "--port", "5050"]
    print(f"[start] backend: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=backend_dir)

if __name__ == "__main__":
    main()