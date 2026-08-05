"""The ledger schema, as SQLAlchemy Core tables.

Architecture §1 in full, plus the Phase 1 additions. Everything here is
deliberately portable: TEXT + CHECK rather than a Postgres ENUM, SQLAlchemy
JSON rather than JSONB, no dialect-specific defaults. The test suite runs the
same code against SQLite and Postgres, and a Postgres-ism leaking in here is
what that run exists to catch.

Money is BIGINT minor units with currency in its own column. Never a float.

**Tenancy.** Every ledger table carries `tenant_id`, and every uniqueness
constraint that could otherwise collide between households is scoped by it.
The system runs single-tenant and single-member for now — one default tenant
is seeded and everything resolves to it — but the *shape* is here from the
start, because adding a tenant scope after a ledger has history means
recomputing every dedupe key in it. See docs/development-rules.md Rule 3.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

PARSE_STATUSES = ("imported", "imported_unverified", "quarantined")
ACCOUNT_KINDS = ("deposit", "card", "loan")
SOURCE_PROFILES = ("dummy", "prod")

#: Roles a member holds within a tenant. `owner` administers the tenant;
#: `adult` has full access to what is shared with them; `child` and `viewer`
#: are read-only. Enforcement belongs to the API layer when it exists — this
#: is the vocabulary it will enforce against.
MEMBER_ROLES = ("owner", "adult", "child", "viewer")
MEMBER_STATUSES = ("active", "invited", "suspended")
TENANT_STATUSES = ("active", "suspended")

#: The tenant and member every single-user installation resolves to.
DEFAULT_TENANT_SLUG = "default"
DEFAULT_MEMBER_EMAIL = "owner@localhost"

# --- Tenancy ----------------------------------------------------------------

tenant = Table(
    "tenant",
    metadata,
    Column("id", Integer, primary_key=True),
    # Stable, URL-safe handle. A household or "family plan".
    Column("slug", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('" + "','".join(TENANT_STATUSES) + "')",
        name="ck_tenant_status",
    ),
)

member = Table(
    "member",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("display_name", String(128), nullable=False),
    Column("email", String(320)),
    # OIDC identity: the issuer and its subject claim, which together are the
    # only globally stable identifier an SSO provider gives you. Email is not
    # an identity — it can be reassigned.
    #
    # No password column exists, and none should be added: authentication is
    # the identity provider's job, and a credential this system never holds is
    # one it can never leak.
    Column("auth_issuer", String(255)),
    Column("auth_subject", String(255)),
    Column("role", String(16), nullable=False, server_default="owner"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # One SSO identity maps to exactly one member. NULLs do not collide, so
    # members can exist before their first login.
    UniqueConstraint("auth_issuer", "auth_subject", name="uq_member_identity"),
    UniqueConstraint("tenant_id", "email", name="uq_member_email_per_tenant"),
    CheckConstraint("role IN ('" + "','".join(MEMBER_ROLES) + "')", name="ck_member_role"),
    CheckConstraint("status IN ('" + "','".join(MEMBER_STATUSES) + "')", name="ck_member_status"),
)

# --- Ledger -----------------------------------------------------------------

source_document = Table(
    "source_document",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    # Who dropped the file in. Null for anything ingested before members
    # existed, or by an automated feed.
    Column("uploaded_by_member_id", Integer, ForeignKey("member.id")),
    Column("sha256", String(64), nullable=False),
    # Identifies the *statement* rather than the file: one statement per
    # account per period, whatever bytes happened to carry it. A re-downloaded
    # or re-saved PDF is the same statement; two members of a household both
    # uploading a shared account's statement produce one, not two.
    Column("statement_key", String(64)),
    Column("institution", String(64), nullable=False),
    Column("doc_type", String(32), nullable=False),
    Column("period_start", Date),
    Column("period_end", Date),
    Column("statement_date", Date),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("fetch_method", String(32), nullable=False, server_default="manual_upload"),
    Column("storage_path", Text, nullable=False),
    Column("parser_version", String(64)),
    Column("layout_fingerprint", String(64)),
    Column("parse_status", String(32), nullable=False),
    # Staging provenance: which profile the file came from and where the
    # operator had it filed.
    Column("source_profile", String(16), nullable=False),
    Column("source_relpath", Text, nullable=False),
    # Half of what makes re-import a guaranteed no-op — scoped to the tenant,
    # so two households holding the same statement do not collide.
    UniqueConstraint("tenant_id", "sha256", name="uq_source_document_sha256"),
    UniqueConstraint("tenant_id", "statement_key", name="uq_source_document_statement"),
    CheckConstraint(
        "parse_status IN ('" + "','".join(PARSE_STATUSES) + "')",
        name="ck_source_document_parse_status",
    ),
    CheckConstraint(
        "source_profile IN ('" + "','".join(SOURCE_PROFILES) + "')",
        name="ck_source_document_source_profile",
    ),
)

account = Table(
    "account",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    # Which member the account belongs to. NULL means it is shared across the
    # whole tenant — a joint account, in family terms. This is what lets one
    # household hold both joint and personal accounts.
    Column("owner_member_id", Integer, ForeignKey("member.id")),
    Column("institution", String(64), nullable=False),
    # The account's stable identity within its institution.
    #
    # For deposit accounts this is the masked account number. For cards it is
    # the *product* — the card brand — and deliberately not the card number:
    # numbers change on reissue or replacement while the account continues, so
    # keying on the number would fork one account's history in two. Two cards
    # held at the same bank are distinguished by product, which is stable for
    # the life of the account.
    Column("account_ref_masked", String(64), nullable=False),
    # Trust savings statements carry several pockets, each with its own
    # balances, inside one document. Each becomes its own account row.
    Column("sub_account_label", String(128), nullable=False, server_default=""),
    Column("currency", String(3), nullable=False),
    Column("kind", String(16), nullable=False),
    UniqueConstraint(
        "tenant_id", "institution", "account_ref_masked", "sub_account_label", "currency",
        name="uq_account_identity",
    ),
    CheckConstraint(
        "kind IN ('" + "','".join(ACCOUNT_KINDS) + "')",
        name="ck_account_kind",
    ),
)

txn = Table(
    "txn",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("account_id", Integer, ForeignKey("account.id"), nullable=False),
    Column("source_document_id", Integer, ForeignKey("source_document.id"), nullable=False),
    Column("posted_date", Date, nullable=False),
    Column("value_date", Date),
    # Signed by effect on the account balance: money in positive, out negative.
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    # Foreign-currency rows keep the originally billed amount and the rate the
    # institution settled at. amount_minor stays the settled amount, so
    # reconciliation is unaffected.
    Column("fx_amount_minor", BigInteger),
    Column("fx_currency", String(3)),
    Column("fx_rate", Numeric(18, 8)),
    Column("description_raw", Text, nullable=False),
    Column("description_norm", Text, nullable=False),
    Column("counterparty_norm", Text, nullable=False, server_default=""),
    Column("seq", Integer, nullable=False, server_default="0"),
    # The other half of idempotency. The key itself is a property of the
    # transaction's content; the tenant scope lives in the constraint, so two
    # households with identical transactions never collide.
    Column("dedupe_key", String(64), nullable=False),
    UniqueConstraint("tenant_id", "dedupe_key", name="uq_txn_dedupe_key"),
)

Index("ix_txn_account_posted", txn.c.account_id, txn.c.posted_date)

#: Two rows proven to be one movement between the household's own accounts.
#:
#: Beside the transactions rather than on them. Both rows are facts about what
#: a statement said and must not be edited; whether a pair is a transfer is a
#: judgement, and judgements get revised. Deleting every row here and running
#: the pass again is a supported operation, which it would not be if the claim
#: lived in a column on `txn`.
#:
#: `out_txn_id` is the leg that lost the money and `in_txn_id` the one that
#: gained it, so the pair reads the way the movement happened.
transfer_link = Table(
    "transfer_link",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("out_txn_id", Integer, ForeignKey("txn.id"), nullable=False),
    Column("in_txn_id", Integer, ForeignKey("txn.id"), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("days_apart", Integer, nullable=False, server_default="0"),
    # Why the pair was accepted, so an operator can audit rather than trust.
    Column("evidence", String(64), nullable=False),
    Column("linked_at", DateTime(timezone=True), nullable=False),
    # A transaction belongs to at most one movement. Without this a rebuild
    # that ran twice would pair the same rows again and quietly double the
    # amount excluded from spending.
    UniqueConstraint("tenant_id", "out_txn_id", name="uq_transfer_link_out"),
    UniqueConstraint("tenant_id", "in_txn_id", name="uq_transfer_link_in"),
)
Index("ix_txn_source_document", txn.c.source_document_id)
Index("ix_txn_tenant", txn.c.tenant_id)

# Per-account opening and closing balances as the statement stated them.
# Without this, the monthly reconciliation in architecture §8.2 has nothing to
# compare the pipeline's own numbers against.
statement_balance = Table(
    "statement_balance",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("account_id", Integer, ForeignKey("account.id"), nullable=False),
    Column("source_document_id", Integer, ForeignKey("source_document.id"), nullable=False),
    Column("opening_balance_minor", BigInteger),
    Column("closing_balance_minor", BigInteger),
    UniqueConstraint("account_id", "source_document_id", name="uq_statement_balance"),
)

# --- Created now, unused until the enrichment phase -------------------------
# The data model is the architecture: separating immutable facts from mutable
# predictions is what lets five years of history be re-classified without
# touching the ledger underneath.

txn_enrichment = Table(
    "txn_enrichment",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("txn_id", Integer, ForeignKey("txn.id"), nullable=False),
    Column("category", String(64)),
    Column("subcategory", String(64)),
    # "For whom" — which member the spend was for. Distinct from the account's
    # owner: a joint card can pay for any member.
    Column("beneficiary_member_id", Integer, ForeignKey("member.id")),
    Column("beneficiary", String(64)),
    Column("confidence", Numeric(5, 4)),
    Column("source", String(16), nullable=False),
    Column("model_version", String(64)),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("source IN ('rule','knn','llm','human')", name="ck_txn_enrichment_source"),
)

recurrence_series = Table(
    "recurrence_series",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Integer, ForeignKey("tenant.id"), nullable=False),
    Column("merchant_norm", Text, nullable=False),
    Column("amount_centre_minor", BigInteger, nullable=False),
    Column("amount_tolerance_minor", BigInteger, nullable=False),
    Column("period_days", Integer, nullable=False),
    Column("period_label", String(32)),
    Column("confidence", Numeric(5, 4)),
    Column("expected_next", Date),
    Column("last_seen", Date),
    Column("member_count", Integer, nullable=False, server_default="0"),
)

txn_series_link = Table(
    "txn_series_link",
    metadata,
    Column("txn_id", Integer, ForeignKey("txn.id"), primary_key=True),
    Column("series_id", Integer, ForeignKey("recurrence_series.id"), primary_key=True),
)

#: Every table carrying tenant-owned rows. Used by the isolation tests, which
#: assert that nothing gains a tenant-scoped row without being listed here.
TENANT_SCOPED_TABLES = (
    source_document,
    account,
    txn,
    transfer_link,
    statement_balance,
    txn_enrichment,
    recurrence_series,
)
