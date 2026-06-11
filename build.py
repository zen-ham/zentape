"""Build zentape into a self-contained --onedir app.

Steps:
  1. cargo build --release  (engine_src) -> engine.exe + zentape_hook.dll
  2. copy those + a real ffmpeg.exe into bin/
  3. PyInstaller zentape.spec -> dist/zentape/zentape.exe

Run:  python build.py
"""
import os
import sys
import shutil
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(ROOT, "bin")
ENGINE_SRC = os.path.join(ROOT, "engine_src")


def sh(cmd, cwd=None):
    print(">", cmd if isinstance(cmd, str) else " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), check=True)


def main():
    os.makedirs(BIN, exist_ok=True)

    # 1. Build the Rust engine + hook DLL.
    print("\n=== [1/3] cargo build --release ===")
    sh(["cargo", "build", "--release"], cwd=ENGINE_SRC)
    rel = os.path.join(ENGINE_SRC, "target", "release")
    for f in ("engine.exe", "zentape_hook.dll"):
        shutil.copy2(os.path.join(rel, f), os.path.join(BIN, f))
        print("  copied", f)

    # 2. Ensure a real (non-shim) ffmpeg.exe is in bin/.
    print("\n=== [2/3] ffmpeg.exe ===")
    ff = os.path.join(BIN, "ffmpeg.exe")
    if os.path.isfile(ff) and os.path.getsize(ff) > 1_000_000:
        print("  ffmpeg.exe already present")
    else:
        cands = [
            r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe",
            r"C:\ProgramData\chocolatey\lib\ffmpeg-full\tools\ffmpeg\bin\ffmpeg.exe",
            shutil.which("ffmpeg"),
        ]
        src = next((c for c in cands
                    if c and os.path.isfile(c) and os.path.getsize(c) > 1_000_000), None)
        if src:
            shutil.copy2(src, ff)
            print("  copied ffmpeg from", src)
        else:
            print("  WARNING: no real ffmpeg.exe found; the app will fall back to "
                  "ffmpeg on PATH at runtime.")

    # 3. PyInstaller.
    print("\n=== [3/3] PyInstaller ===")
    sh([sys.executable, "-m", "PyInstaller", "--noconfirm", "zentape.spec"], cwd=ROOT)

    out = os.path.join(ROOT, "dist", "zentape", "zentape.exe")
    print("\nDone ->", out if os.path.isfile(out) else "(build did not produce exe)")


if __name__ == "__main__":
    main()
