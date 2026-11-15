"""The `.logicx` package: a folder holding `ProjectData` plus plists and media.

    Song.logicx/
      Resources/ProjectInformation.plist
      Alternatives/000/ProjectData        <- the binary project (see container.py)
      Alternatives/000/MetaData.plist     <- cached summary (tempo, signature, ...)
      Alternatives/000/DisplayState.plist
      Media/Audio Files/...
"""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path
from typing import Any

from .container import ProjectData

ALTERNATIVE = Path("Alternatives") / "000"

# Logic keeps old versions here; a copied template should not carry them.
_SKIP_ON_COPY = ("Project File Backups", "Autosave")


class BundleError(Exception):
    pass


class Bundle:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not (self.path / ALTERNATIVE / "ProjectData").is_file():
            raise BundleError(f"{self.path} is not a Logic project (no {ALTERNATIVE}/ProjectData)")

    @property
    def project_data_path(self) -> Path:
        return self.path / ALTERNATIVE / "ProjectData"

    @property
    def metadata_path(self) -> Path:
        return self.path / ALTERNATIVE / "MetaData.plist"

    def read(self) -> ProjectData:
        return ProjectData.parse(self.project_data_path.read_bytes())

    def write(self, pd: ProjectData) -> None:
        self.project_data_path.write_bytes(pd.serialize())

    def metadata(self) -> dict[str, Any]:
        if not self.metadata_path.is_file():
            return {}
        with self.metadata_path.open("rb") as f:
            return plistlib.load(f)

    def update_metadata(self, **values: Any) -> None:
        """Update `MetaData.plist`. Logic treats it as a cache (the authoritative
        values are in `ProjectData`), but keeping it in sync avoids stale info in
        the Finder and in Logic's project browser."""
        if not self.metadata_path.is_file():
            return
        meta = self.metadata()
        meta.update(values)
        with self.metadata_path.open("wb") as f:
            plistlib.dump(meta, f, fmt=plistlib.FMT_BINARY)

    @classmethod
    def copy(cls, src: Path | str, dst: Path | str, *, overwrite: bool = False) -> Bundle:
        src_b = cls(src)
        dst = Path(dst)
        if dst.exists():
            if not overwrite:
                raise BundleError(f"{dst} already exists (pass --force to overwrite)")
            shutil.rmtree(dst)
        shutil.copytree(src_b.path, dst, ignore=shutil.ignore_patterns(*_SKIP_ON_COPY))
        return cls(dst)
