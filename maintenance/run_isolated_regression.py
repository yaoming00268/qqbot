"""Run every root test module in its own guarded pytest process (Qt/native isolation)."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 5))
    parser.add_argument("--rerun-failed", action="store_true", help="保留上次证据，只复验失败模块并重建总表")
    args = parser.parse_args()
    report = args.report.resolve()
    report.mkdir(parents=True, exist_ok=True)
    modules = sorted(ROOT.glob("test_*.py"))
    total_modules = len(modules)
    results, previous_seconds, history = [], 0, []
    if args.rerun_failed:
        previous = json.loads((report / "summary.json").read_text(encoding="utf-8"))
        failed = {row["module"] for row in previous["results"] if not row["ok"]}
        modules = [module for module in modules if module.name in failed]
        results = [row for row in previous["results"] if row["ok"]]
        previous_seconds = previous["seconds"]
        history = previous.get("reruns", [])
        archive = report / ("before-rerun-" + str(len(history) + 1))
        archive.mkdir()
        for path in [report / "summary.json", report / "all-tests.xml", *[report / (Path(name).stem + suffix)
                for name in failed for suffix in (".xml", ".log")]]:
            if path.is_file():
                shutil.copy2(path, archive / path.name)
        history.append({"modules": sorted(failed), "previous_evidence": str(archive)})
    started = time.monotonic()
    def check(path):
        began = time.monotonic()
        xml = report / (path.stem + ".xml")
        command = [sys.executable, "-m", "pytest", "-q", path.name, "--junitxml=" + str(xml)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (report / (path.stem + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        # Fixture-only modules may legitimately collect zero cases.
        return {"module": path.name, "exit_code": result.returncode, "ok": result.returncode in (0, 5),
                "seconds": time.monotonic() - began, "xml": str(xml),
                **({"error": (result.stdout + result.stderr)[-3500:]} if result.returncode not in (0, 5) else {})}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, module): module for module in modules}
        for future in as_completed(futures):
            try:
                value = future.result()
            except Exception as error:
                value = {"module": futures[future].name, "ok": False, "error": str(error)}
            results.append(value)
            print(json.dumps(value, ensure_ascii=True), flush=True)
    suites = ET.Element("testsuites")
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for result in results:
        xml = Path(result.get("xml", ""))
        if xml.is_file():
            for suite in ET.parse(xml).getroot().iter("testsuite"):
                suites.append(suite)
                for key in counts:
                    counts[key] += int(suite.get(key, "0"))
    ET.ElementTree(suites).write(report / "all-tests.xml", encoding="utf-8", xml_declaration=True)
    summary = {"ok": all(value["ok"] for value in results) and len(results) == total_modules, "modules": total_modules,
        "mode": "one guarded pytest process per test module", "seconds": previous_seconds + time.monotonic() - started,
        "reruns": history,
        **counts, "results": sorted(results, key=lambda row: row["module"])}
    (report / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
