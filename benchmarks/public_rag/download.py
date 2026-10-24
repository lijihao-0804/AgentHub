"""Acquire pinned public resources. Never import or execute downloaded code."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import tarfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Path escapes resource directory: {relative}")
    return path


def digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def unpack(path: Path, target: Path, kind: str) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if item.is_dir() or "__MACOSX" in Path(item.filename).parts:
                    continue
                if stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError("Archive symlinks are not allowed")
                dest = contained(target.resolve(), item.filename)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
    elif kind == "tar":
        with tarfile.open(path) as archive:
            for item in archive:
                if item.isdir():
                    continue
                if not item.isfile():
                    raise ValueError("Archive special files are not allowed")
                dest = contained(target.resolve(), item.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
    else:
        raise ValueError(f"Unknown archive kind: {kind}")


def acquire(asset: dict, root: Path, previous: dict) -> dict:
    path = contained(root, asset["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = previous.get(asset["path"], {})
    cached = path.is_file() and prior.get("sha256") == digest(path)
    effective_url = prior.get("effective_url", asset["url"])
    if not cached:
        temporary = path.with_name(path.name + ".part")
        for attempt in range(3):
            try:
                with requests.get(
                    asset["url"],
                    stream=True,
                    timeout=(30, 120),
                    headers={"User-Agent": "AgentHub-public-resource-downloader/1"},
                ) as response:
                    response.raise_for_status()
                    if "text/html" in response.headers.get("Content-Type", "") and asset.get(
                        "archive"
                    ):
                        raise ValueError("Archive URL returned an HTML page")
                    with temporary.open("wb") as out:
                        for block in response.iter_content(1024 * 1024):
                            out.write(block)
                    effective_url = response.url
                if asset.get("md5") and digest(temporary, "md5") != asset["md5"]:
                    raise ValueError("Upstream archive MD5 mismatch")
                if prior.get("sha256") and digest(temporary) != prior["sha256"]:
                    raise ValueError("Downloaded content differs from recorded SHA-256")
                if asset.get("archive") == "zip" and not zipfile.is_zipfile(temporary):
                    raise ValueError("Downloaded file is not a ZIP archive")
                temporary.replace(path)
                break
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
    if asset.get("md5") and digest(path, "md5") != asset["md5"]:
        raise ValueError("Cached archive MD5 mismatch")
    if asset.get("archive"):
        unpack(path, contained(root, asset["extract_to"]), asset["archive"])
    return {
        "path": asset["path"],
        "url": asset["url"],
        # Signed CDN URLs are transient; preserve stable source URL in the manifest.
        "effective_host": requests.utils.urlparse(effective_url).netloc,
        "bytes": path.stat().st_size,
        "sha256": digest(path),
        "status": "ACQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=HERE / "sources.json")
    parser.add_argument("--manifest", type=Path, default=HERE / "acquisition.json")
    args = parser.parse_args()
    spec = json.loads(args.sources.read_text(encoding="utf-8"))
    root = contained(ROOT, spec["local_root"])
    root.mkdir(parents=True, exist_ok=True)
    previous = {}
    if args.manifest.exists():
        previous = {
            a["path"]: a
            for a in json.loads(args.manifest.read_text(encoding="utf-8"))["assets"]
        }
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(acquire, asset, root, previous): asset for asset in spec["assets"]}
        for future in as_completed(pending):
            asset = pending[future]
            try:
                result = future.result()
                print(f"ACQUIRED {result['path']} ({result['bytes']} bytes)", flush=True)
            except Exception as exc:
                result = {"path": asset["path"], "status": "FAILED", "error": str(exc)}
                print(f"FAILED {asset['path']}: {exc}", flush=True)
            results.append(result)
    inventory = [
        {"path": p.relative_to(root).as_posix(), "bytes": p.stat().st_size, "sha256": digest(p)}
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(".part")
    ]
    failed = any(result["status"] != "ACQUIRED" for result in results)
    manifest = {
        "schema_version": 1,
        "acquired_at_utc": datetime.now(UTC).isoformat(),
        "local_root": spec["local_root"],
        "upstream_revisions": spec["upstream_revisions"],
        "status": "INCOMPLETE" if failed else "ACQUIRED",
        "experiments_run": False,
        "tool_dependencies_installed": False,
        "assets": sorted(results, key=lambda item: item["path"]),
        "inventory": inventory,
    }
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
