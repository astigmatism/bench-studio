"""Copy only bounded, regular source files. Never follow generated symlinks."""

import os
import sys
import zipfile
from pathlib import Path

root = Path("/workspace")
allowed = {"backend", "frontend", "tests", "docs"}
skip = {"node_modules", "dist", ".git", "__pycache__", ".pytest_cache"}
count = total = 0
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as archive:
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip]
        for d in dirs:
            if (Path(base) / d).is_symlink():
                raise ValueError("Symlink in candidate sources")
        for name in files:
            path = Path(base) / name
            rel = path.relative_to(root)
            if rel.parts[0] not in allowed and str(rel) not in {
                "README.md",
                "prototype.html",
                "app.json",
                ".gitignore",
            }:
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError("Non-regular candidate file")
            size = path.stat().st_size
            total += size
            count += 1
            if size > 8 * 1024 * 1024 or total > 32 * 1024 * 1024 or count > 500:
                raise ValueError("Candidate exceeds source artifact limits")
            archive.write(path, str(rel))
