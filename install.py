# Run by ComfyUI-Manager after install: builds the yue2.cpp binaries if they are missing.
import os, shutil, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
if all(os.path.exists(os.path.join(here, "bin", b)) for b in ("yue-synth", "yue-plan", "yue-transcribe", "yue-tokenize")):
    sys.exit(0)
if not shutil.which("cmake"):
    print("[YuE2-Mac] cmake not found. Run: brew install cmake && xcode-select --install, then bash build.sh")
    sys.exit(0)
subprocess.run(["bash", os.path.join(here, "build.sh")], check=False)
