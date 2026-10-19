#!/usr/bin/env python3
"""Build a reproducible, dependency-free ZIP for Chrome's Load unpacked command."""

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "apps" / "extension"
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["manifest_version"] != 3:
        raise ValueError("The production extension must use Manifest V3")
    files = sorted(
        path for path in source.iterdir()
        if path.suffix in {".js", ".html", ".css"} or path.name == "manifest.json"
    )
    destination = root / "dist" / "language-tutor-extension.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            entry = ZipInfo(path.name, date_time=(2026, 10, 19, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, path.read_bytes(), compresslevel=9)
    print(destination)
    print("SHA-256:", hashlib.sha256(destination.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
