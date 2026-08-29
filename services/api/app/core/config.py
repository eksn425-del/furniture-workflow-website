from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT.parent / "output" / "web_projects"


def _validated_web_origin(value: str) -> str:
    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WEB_BASE_URL must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("WEB_BASE_URL must not contain a path, query, or fragment")
    return origin


def _local_origin_aliases(origin: str) -> tuple[str, ...]:
    parsed = urlsplit(origin)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return (origin,)
    port = f":{parsed.port}" if parsed.port is not None else ""
    aliases = (
        f"{parsed.scheme}://localhost{port}",
        f"{parsed.scheme}://127.0.0.1{port}",
    )
    return tuple(dict.fromkeys((origin, *aliases)))


@dataclass(frozen=True, slots=True)
class Settings:
    output_root: Path
    web_base_url: str = "http://localhost:3000"
    api_prefix: str = "/api/v1"
    database_filename: str = "control_plane.sqlite3"
    scrape_poll_seconds: float = 2.0
    scrape_lease_seconds: int = 900

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser().resolve())
        object.__setattr__(self, "web_base_url", _validated_web_origin(self.web_base_url))
        if Path(self.database_filename).name != self.database_filename:
            raise ValueError("database_filename must be a plain file name")
        if not 0.2 <= self.scrape_poll_seconds <= 60:
            raise ValueError("scrape_poll_seconds must be between 0.2 and 60")
        if not 60 <= self.scrape_lease_seconds <= 86_400:
            raise ValueError("scrape_lease_seconds must be between 60 and 86400")

    @property
    def database_path(self) -> Path:
        return self.output_root / "_system" / self.database_filename

    @property
    def web_allowed_origins(self) -> tuple[str, ...]:
        return _local_origin_aliases(self.web_base_url)

    @classmethod
    def from_environment(
        cls,
        *,
        load_env_file: bool,
        repository_root: Path = REPOSITORY_ROOT,
    ) -> "Settings":
        if load_env_file:
            root = Path(repository_root).resolve()
            try:
                load_dotenv(root.parent / ".env.local", override=False)
                load_dotenv(root / ".env.local", override=False)
            except OSError:
                pass
        configured_output = os.getenv("OUTPUT_ROOT", "").strip()
        return cls(
            output_root=Path(configured_output) if configured_output else DEFAULT_OUTPUT_ROOT,
            web_base_url=os.getenv("WEB_BASE_URL", "http://localhost:3000"),
            scrape_poll_seconds=float(os.getenv("SCRAPE_POLL_SECONDS", "2")),
            scrape_lease_seconds=int(os.getenv("SCRAPE_LEASE_SECONDS", "900")),
        )


def feature_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["DEFAULT_OUTPUT_ROOT", "REPOSITORY_ROOT", "Settings", "feature_flag"]
