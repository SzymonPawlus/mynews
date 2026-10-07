"""Paths, YAML config and .env loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("MYNEWS_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
PROMPTS_DIR = ROOT / "prompts"
TEMPLATES_DIR = ROOT / "templates"
DB_PATH = DATA_DIR / "mynews.sqlite"

SECTIONS = ("ai", "economy", "science", "world")
SECTION_TITLES = {
    "ai": "AI progress",
    "economy": "Economy",
    "science": "Science",
    "world": "World",
}


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass
class Source:
    name: str
    section: str
    kind: str
    weight: float = 0.5
    url: str | None = None
    cadence: str = "daily"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_items: int | None = None
    enabled: bool = True
    query_section: str | None = None
    original_link: bool = False  # use first outbound link in entry body (link blogs)

    @property
    def weekly(self) -> bool:
        return self.cadence == "weekly"


def load_sources() -> list[Source]:
    raw = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())
    sources = [Source(**s) for s in raw["sources"]]
    for s in sources:
        if s.section not in SECTIONS:
            raise ValueError(f"source {s.name}: unknown section {s.section!r}")
    return [s for s in sources if s.enabled]


def load_profile() -> dict:
    return yaml.safe_load((CONFIG_DIR / "profile.yaml").read_text())
