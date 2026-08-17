"""Runtime configuration and paths."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

# Governance rule 1: only approved government hosts may ever be crawled or
# downloaded from. This allowlist is checked when a source is registered AND
# again at download time, so a redirect off-domain cannot smuggle a file in.
# Suffix match on the registrable host, e.g. "cms.tn.gov.in" matches "tn.gov.in".
APPROVED_HOST_SUFFIXES: tuple[str, ...] = (
    "tn.gov.in",
    "tnega.org",
    "nic.in",
    "gov.in",
)

# Hosts that look governmental but are aggregators/news mirrors. Rejected even
# if they end with an approved suffix.
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "news.gov.in",
        "pib.gov.in",  # press releases, not the order itself
    }
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Deployment mode. "development" (default) is what every local/demo run uses.
# Set THIRDEYE_ENV=production when serving over HTTPS behind a real domain --
# this is the one flag a deployment must remember to flip, since it controls
# the session cookie's Secure attribute (see workbench/app.py). Cookies can't
# be marked Secure in local dev because plain http://localhost has no TLS.
ENVIRONMENT = os.environ.get("THIRDEYE_ENV", "development")
COOKIE_SECURE = ENVIRONMENT == "production"

# Politeness: seconds between requests to the same host.
CRAWL_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # 100 MB guard against runaway downloads


# ---------------------------------------------------------------------------
# Phase 2 -- OCR (Module 4)
# ---------------------------------------------------------------------------
def _default_tesseract_cmd() -> str | None:
    env = os.environ.get("THIRDEYE_TESSERACT_CMD")
    if env:
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    windows_default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if windows_default.exists():
        return str(windows_default)
    return None


TESSERACT_CMD = _default_tesseract_cmd()
# Project-local by default: a self-contained tessdata dir (eng + tam) travels
# with the repo instead of depending on wherever Tesseract itself was
# installed, so a fresh checkout only needs the binary, not language-pack setup.
TESSDATA_DIR = Path(os.environ.get("THIRDEYE_TESSDATA_DIR") or PROJECT_ROOT / "vendor" / "tessdata")
OCR_LANGUAGES = "eng+tam"
OCR_DPI = 300
# Below this mean word confidence (0..1), an OCR page is judged unusable and
# the sparse digital text is kept rather than replaced with likely garbage.
OCR_MIN_CONFIDENCE = 0.35


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    repository_dir: Path

    @classmethod
    def load(cls, data_dir: str | os.PathLike[str] | None = None) -> "Settings":
        root = Path(data_dir or os.environ.get("THIRDEYE_DATA_DIR") or PROJECT_ROOT / "data")
        root = root.resolve()
        return cls(
            data_dir=root,
            db_path=root / "thirdeye.db",
            repository_dir=root / "documents",
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repository_dir.mkdir(parents=True, exist_ok=True)
