#!/usr/bin/env python3
"""Prepare licensed subtitle files for the manual Chrome acceptance checklist."""

import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "data" / "demo"
    destination = root / "dist" / "chrome-acceptance"
    destination.mkdir(parents=True, exist_ok=True)
    srt = (source / "elephants-dream.en.srt").read_text(encoding="utf-8")

    def vtt(content: str) -> str:
        return "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)

    files = {
        "full.srt": srt,
        "full.vtt": vtt(srt),
        "partial.vtt": vtt("\n\n".join(srt.strip().split("\n\n")[:3]) + "\n"),
        "invalid.srt": "This is an intentionally invalid subtitle file.\n",
        "empty.srt": "",
        "SOURCE.md": (source / "README.md").read_text(encoding="utf-8")
        + "\n## Acceptance copies\n\n"
        + "full.srt retains all licensed cues. full.vtt changes only the format header and "
        + "timestamp separators. partial.vtt contains only the first three licensed cues; "
        + "import it with completeness unchecked. invalid.srt and empty.srt are original "
        + "validation fixtures, not video subtitles. Use video TLkA0RELQ1g.\n",
    }
    archive_path = destination.parent / "chrome-acceptance-files.zip"
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            data = content.encode("utf-8")
            (destination / name).write_bytes(data)
            entry = ZipInfo(name, date_time=(2026, 10, 20, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data, compresslevel=9)
    print(destination)
    print(archive_path)


if __name__ == "__main__":
    main()
