"""Constants, secret lookup and paths. Nothing here performs network calls."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendor endpoints
OPENSANCTIONS_BASE = "https://api.opensanctions.org"
NAMESCAN_BASE = "https://api.namescan.io/v3.1"
GLEIF_BASE = "https://api.gleif.org/api/v1"
COMPANIES_HOUSE_BASE = "https://api.company-information.service.gov.uk"

# Layer A defaults (vendor spec §2, §11)
OS_DATASET = "default"
OS_ALGORITHM = "best"
OS_THRESHOLD = 0.7
OS_LIMIT = 10

# Layer C defaults (vendor spec §7)
NS_MATCH_RATE = 75
NS_MAX_RESULTS = 100
NS_COST_WITH_MEDIA = 1.25
NS_COST_NO_MEDIA = 1.0
NS_LOW_CREDIT_WARN = 20
NS_MAX_RETRIES = 2
NS_TIMEOUT_MEDIA = 60.0
DEFAULT_TIMEOUT = 30.0

# Retention (vendor spec §11)
DEDUP_DAYS = 90
VENDOR_TEXT_DAYS = 90
RECORD_RETENTION_DAYS = 365

SECRET_NAMES = (
    "OPENSANCTIONS_API_KEY",
    "NAMESCAN_API_KEY",
    "NAMESCAN_API_KEY_TEST",
    "COMPANIES_HOUSE_API_KEY",
)


def get_secret(name: str) -> str | None:
    """Environment variable first, then macOS Keychain generic password with service `name`."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", name, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip() or None


def cases_dir() -> Path:
    return Path(os.environ.get("SCREENING_CASES_DIR", REPO_ROOT / "cases"))


def max_subjects_run() -> int:
    return int(os.environ.get("NAMESCAN_MAX_SUBJECTS_RUN", "20"))
