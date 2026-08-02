"""Configuration, entirely from the environment.

No implementation choice is hardcoded anywhere else: the database engine, the
storage root and the notifier are all selected here, which is what makes the
seams in app/ports/ actually swappable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROFILE_DUMMY = "dummy"
PROFILE_PROD = "prod"
PROFILES = (PROFILE_DUMMY, PROFILE_PROD)

#: Extensions the stage step will pick up. Anything else in uploads/ is the
#: operator's business and is left alone.
DOCUMENT_EXTENSIONS = (".pdf", ".csv", ".ofx", ".qfx", ".qif", ".sta", ".mt940", ".xml")


class ConfigError(RuntimeError):
    pass


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


#: The tenant and member a single-user installation resolves to. Real values
#: arrive from an authenticated session once SSO exists; until then everything
#: runs as this one household with one member.
DEFAULT_TENANT_SLUG = "default"
DEFAULT_MEMBER_EMAIL = "owner@localhost"


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str
    uploads_dir: Path
    data_dir: Path
    allow_prod: bool
    amount_ceiling_minor: int
    tenant_slug: str = DEFAULT_TENANT_SLUG
    member_email: str | None = DEFAULT_MEMBER_EMAIL

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "store"

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / "quarantine"

    @property
    def reports_dir(self) -> Path:
        """Where a run leaves a shareable write-up of its failures."""
        return self.data_dir / "reports"

    def uploads_for(self, profile: str) -> Path:
        if profile not in PROFILES:
            raise ConfigError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        return self.uploads_dir / profile

    def require_profile_allowed(self, profile: str) -> None:
        """Refuse to touch real data unless the operator opted in explicitly.

        uploads/prod holds real financial statements. Processing it must be a
        deliberate act, never muscle memory. See docs/development-rules.md
        Rule 2.
        """
        if profile == PROFILE_PROD and not self.allow_prod:
            raise ConfigError(
                "refusing to process uploads/prod: set FINSTONE_ALLOW_PROD=1 to confirm. "
                "This folder holds real financial data and agents must never read it."
            )

    def pdf_password_for(self, institution: str) -> str | None:
        """Per-institution PDF password, if one is configured.

        Some e-statements are user-password protected (typically an NRIC or
        date of birth). The dummy files are only owner-restricted and open
        with an empty password, but prod files may not be.

        Looked up as FINSTONE_PDF_PASSWORD_<INSTITUTION>, upper-cased with
        non-alphanumerics collapsed to underscores, falling back to
        FINSTONE_PDF_PASSWORD for a single shared password.
        """
        key = "".join(c if c.isalnum() else "_" for c in institution).upper().strip("_")
        return os.environ.get(f"FINSTONE_PDF_PASSWORD_{key}") or os.environ.get("FINSTONE_PDF_PASSWORD")


def load_config() -> Config:
    data_dir = _path("FINSTONE_DATA_DIR", REPO_ROOT / "data")
    return Config(
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{(data_dir / 'finstone.db').as_posix()}"),
        uploads_dir=_path("FINSTONE_UPLOADS_DIR", REPO_ROOT / "uploads"),
        data_dir=data_dir,
        allow_prod=os.environ.get("FINSTONE_ALLOW_PROD", "") == "1",
        # 10 million in minor units. A personal statement line above this is
        # far more likely to be a misparse than a real transaction.
        amount_ceiling_minor=int(os.environ.get("FINSTONE_AMOUNT_CEILING_MINOR", str(10_000_000_00))),
        # Single-tenant today. These exist so the pipeline is already written
        # against a tenant context, and switching multi-tenancy on becomes a
        # change to how the context is resolved rather than to every call site.
        tenant_slug=os.environ.get("FINSTONE_TENANT", DEFAULT_TENANT_SLUG),
        member_email=os.environ.get("FINSTONE_MEMBER", DEFAULT_MEMBER_EMAIL) or None,
    )
