"""Configuration, entirely from the environment.

No implementation choice is hardcoded anywhere else: the database engine, the
storage root and the notifier are all selected here, which is what makes the
seams in app/ports/ actually swappable.
"""

from __future__ import annotations

import os
import re
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


def _slug(value: str) -> str:
    """A tenant slug reduced to one safe path segment.

    Tenant slugs arrive from the environment today and from a provisioning
    system later, so neither is trusted to be a legal directory name.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "-", value).strip("-.") or DEFAULT_TENANT_SLUG


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

    def quarantine_dir_for(self, profile: str) -> Path:
        """One tenant's quarantine.

        Split by tenant for the same reason the ledger is. A reason file is not
        a bare error code: it carries the document's real filename, the rows
        parsed out of it, its balances and its account references. That is
        precisely the data `tenant_for` exists to keep apart, and leaving it in
        one shared directory would undo the separation everywhere else.
        """
        return self.quarantine_dir / _slug(self.tenant_for(profile))

    def quarantine_files_dir_for(self, profile: str) -> Path:
        """Where `finstone quarantine --export` puts openable originals.

        Holds real statements under recognisable names, which is the whole point
        and also why it is denied to agents alongside uploads/prod — see
        the development rules in README.md, Rule 2.
        """
        return self.quarantine_dir_for(profile) / "files"

    def reports_dir_for(self, profile: str) -> Path:
        """One tenant's run reports. Redacted, but still that tenant's failures."""
        return self.reports_dir / _slug(self.tenant_for(profile))

    @property
    def learned_rules_path(self) -> Path:
        """Vendor strings proven to belong to an adapter.

        Operator state, not code: deleting it costs only the shortcut, since
        anything in it is relearned the next time such a statement arrives.
        """
        return self.data_dir / "learned-layouts.json"

    @property
    def reports_dir(self) -> Path:
        """Where a run leaves a shareable write-up of its failures."""
        return self.data_dir / "reports"

    def uploads_for(self, profile: str) -> Path:
        if profile not in PROFILES:
            raise ConfigError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        return self.uploads_dir / profile

    def tenant_for(self, profile: str) -> str:
        """The tenant a profile's documents belong to.

        Dummy documents live in their own tenant, so as far as production is
        concerned they do not exist: not in its counts, not in its accounts,
        and — the part that actually bit — not in its deduplication.

        Sharing a tenant meant a real statement whose synthetic copy had
        already been imported arrived with every row deduplicated away, leaving
        a document with balances and no transactions.

        A tenant is precisely "a set of records that must never mix", which is
        exactly the requirement here, so the isolation reuses the mechanism
        that already exists and is already tested rather than inventing a
        second one.
        """
        if profile not in PROFILES:
            raise ConfigError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        return self.tenant_slug if profile == PROFILE_PROD else f"{self.tenant_slug}-{profile}"

    def require_profile_allowed(self, profile: str) -> None:
        """Refuse to touch real data unless the operator opted in explicitly.

        uploads/prod holds real financial statements. Processing it must be a
        deliberate act, never muscle memory. See the development rules in README.md
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
