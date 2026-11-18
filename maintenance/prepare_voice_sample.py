"""Download the explicitly requested noncommercial ATRI sample and base encoders."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from voice_setup import download_sample
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("QQBOT_HOME") or ROOT))
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    print(download_sample(home, home / "tools/voice-engine/GPT-SoVITS-Inference"), flush=True)


if __name__ == "__main__":
    main()
