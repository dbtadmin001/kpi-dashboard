"""Fetch only reviewed, SHA256-locked JVM artifacts during image construction."""
import hashlib
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]


def main():
    for artifact in json.loads((ROOT / "delivery/jars.lock.json").read_text()):
        response = requests.get(artifact["url"], timeout=180)
        response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest() != artifact["sha256"]:
            raise RuntimeError(f"Checksum mismatch: {artifact['name']}")
        target = ROOT / artifact["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        print("Verified", artifact["name"])


if __name__ == "__main__":
    main()
