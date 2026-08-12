"""Runtime configuration and paths."""

from __future__ import annotations

import os
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
    "Thirdeye-GO-Intelligence/0.1 (public-interest transparency archive; "
    "contact: admin@thirdeye.local)"
)

# Politeness: seconds between requests to the same host.
CRAWL_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024  # 100 MB guard against runaway downloads


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
