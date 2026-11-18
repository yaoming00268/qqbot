"""Install pinned browser renderer and official Hiyori sample (not a user avatar)."""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
from pathlib import Path
import requests
import time

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "ui/web/vtuber_assets"
SAMPLE_REF = "4-r.7"
BASE = f"https://raw.githubusercontent.com/Live2D/CubismWebSamples/{SAMPLE_REF}/"

def fetch(url):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 QQBot-Live2D-Setup/1.0"})
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == 2: raise
            time.sleep(0.5)

def main():
    files = {
        "vendor/pixi.min.js": "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js",
        "vendor/pixi-live2d-display.min.js": "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js",
        "vendor/live2dcubismcore.min.js": "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js",
        "licenses/pixi-LICENSE": "https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/LICENSE",
        "licenses/pixi-live2d-display-LICENSE": "https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/LICENSE",
        "licenses/CubismWebSamples-LICENSE.md": BASE + "LICENSE.md",
        "licenses/CubismWebSamples-NOTICE.md": BASE + "NOTICE.md",
        "licenses/Live2D-Proprietary-Software-License.html": "https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html",
        "licenses/Live2D-Free-Material-License.html": "https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html",
        "licenses/Live2D-Sample-Model-Terms.html": "https://www.live2d.com/eula/live2d-sample-model-terms_en.html",
    }
    model_url = BASE + "Samples/Resources/Hiyori/"
    raw = fetch(model_url + "Hiyori.model3.json")
    manifest = json.loads(raw)
    references = manifest["FileReferences"]
    paths = {"Hiyori.model3.json"}
    for value in references.values():
        if isinstance(value, str): paths.add(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str): paths.add(item)
                elif isinstance(item, dict) and item.get("File"): paths.add(item["File"])
        elif isinstance(value, dict):
            for entries in value.values():
                for item in entries:
                    if item.get("File"): paths.add(item["File"])
    for path in paths:
        if ".." in Path(path).parts or Path(path).is_absolute(): raise ValueError(path)
        files["models/Hiyori/" + path] = model_url + path
    def install(item):
        name, url = item
        target = DEST / name
        try:
            data = fetch(url)
        except Exception as exc:
            raise RuntimeError(f"Could not download {name} from {url}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": name, "url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(install, files.items()))
    (DEST / "manifest.json").write_text(json.dumps({"sample": "Official Hiyori demonstration only", "sample_ref": SAMPLE_REF, "files": results}, indent=2), encoding="utf-8")
    print(json.dumps({"installed": len(results), "bytes": sum(x["bytes"] for x in results), "directory": str(DEST)}))

if __name__ == "__main__": main()
