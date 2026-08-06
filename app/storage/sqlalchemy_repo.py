"""LedgerRepository on SQLAlchemy Core.

Core rather than the ORM: it gives dialect portability at close to raw-driver
speed, while the ORM's identity map and unit of work would cost more than they
return on a write-mostly ingestion path.

Everything here is portable SQL. Deduplication is a *select existing keys â†’
insert the complement* with the unique index as a backstop, deliberately not
`ON CONFLICT`, so SQLite and Postgres run identical code and the dual-engine
test run stays meaningful. See docs/development-rules.md Rule 1.

**Every read and write is filtered by `context.tenant_id`.** There is no code
path in this class that touches a ledger table without that filter, and the
tenant-isolation tests exist to keep it that way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, cast, create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..domain.tenancy import MemberIdentity, TenantContext
from ..ports.repository import (
    AccountRecord,
    BalanceRecord,
    DocumentRecord,
    InsertResult,
    StatusCounts,
    TxnRecord,
)
from . import schema

#: Chunk size for the "which of these keys already exist" query. Keeps the
#: parameter list well inside every driver's limit.
_KEY_CHUNK = 500


def sum_minor(column):
    """SUM over minor units, as an integer on every engine.

    Postgres widens `SUM(bigint)` to `numeric` to guarantee it cannot overflow,
    and psycopg faithfully returns that as a `Decimal`. SQLite returns a plain
    int. Left alone, the same ledger answers `/summary` with a JSON number on
    one engine and a **string** on the other — which is what a household's
    total spending did the first time it moved to Postgres, breaking the
    contract's promise that money is integer minor units in every field.

    Cast in SQL rather than coerced in Python so it is impossible to read a row
    from one of these without the fix. A household's totals are nowhere near
    a bigint, so the narrowing cannot lose anything.
    """
    return cast(func.sum(column), BigInteger)


class SqlAlchemyLedgerRepository:
    """Works against any engine SQLAlchemy supports; tested on Postgres and SQLite."""

    def __init__(self, url: str, *, echo: bool = False, engine: Engine | None = None):
        self._engine = engine or create_engine(url, echo=echo, future=True)

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_schema(self) -> None:
        schema.metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    # -- tenancy -------------------------------------------------------------

    def ensure_tenant(self, slug: str, name: str | None = None) -> int:
        with self._engine.begin() as conn:
            found = conn.execute(
                select(schema.tenant.c.id).where(schema.tenant.c.slug == slug)
            ).scalar_one_or_none()
            if found is not None:
                return found
            return conn.execute(
                schema.tenant.insert().values(
                    slug=slug,
                    name=name or slug.replace("-", " ").title(),
                    status="active",
                    created_at=datetime.now(timezone.utc),
                )
            ).inserted_primary_key[0]

    def ensure_member(
        self,
        tenant_id: int,
        *,
        display_name: str,
        email: str | None = None,
        identity: MemberIdentity | None = None,
        role: str = "owner",
    ) -> int:
        with self._engine.begin() as conn:
            if identity is not None:
                found = conn.execute(
                    select(schema.member.c.id).where(
                        (schema.member.c.auth_issuer == identity.issuer)
                        & (schema.member.c.auth_subject == identity.subject)
                    )
                ).scalar_one_or_none()
                if found is not None:
                    return found

            if email is not None:
                found = conn.execute(
                    select(schema.member.c.id).where(
                        (schema.member.c.tenant_id == tenant_id)
                        & (schema.member.c.email == email)
                    )
                ).scalar_one_or_none()
                if found is not None:
                    # First SSO login for a member invited by email: attach the
                    # identity rather than creating a second row for them.
                    if identity is not None:
                        conn.execute(
                            schema.member.update()
                            .where(schema.member.c.id == found)
                            .values(auth_issuer=identity.issuer, auth_subject=identity.subject)
                        )
                    return found

            return conn.execute(
                schema.member.insert().values(
                    tenant_id=tenant_id,
                    display_name=display_name,
                    email=email,
                    auth_issuer=identity.issuer if identity else None,
                    auth_subject=identity.subject if identity else None,
                    role=role,
                    status="active",
                    created_at=datetime.now(timezone.utc),
                )
            ).inserted_primary_key[0]

    def resolve_context(self, tenant_slug: str, member_email: str | None = None) -> TenantContext:
        tenant_id = self.ensure_tenant(tenant_slug)
        member_id = None
        role = "owner"
        if member_email:
            member_id = self.ensure_member(
                tenant_id,
                display_name=member_email.split("@")[0],
                email=member_email,
            )
            with self._engine.connect() as conn:
                role = conn.execute(
                    select(schema.member.c.role).where(schema.member.c.id == member_id)
                ).scalar_one()
        return TenantContext(tenant_id=tenant_id, member_id=member_id, role=role)

    def find_member_by_identity(self, identity: MemberIdentity) -> TenantContext | None:
        stmt = select(
            schema.member.c.id, schema.member.c.tenant_id, schema.member.c.role,
        ).where(
            (schema.member.c.auth_issuer == identity.issuer)
            & (schema.member.c.auth_subject == identity.subject)
            & (schema.member.c.status == "active")
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        if row is None:
            return None
        return TenantContext(tenant_id=row.tenant_id, member_id=row.id, role=row.role)

    # -- reads ---------------------------------------------------------------

    def get_document_id(self, context: TenantContext, sha256: str) -> int | None:
        stmt = select(schema.source_document.c.id).where(
            (schema.source_document.c.tenant_id == context.tenant_id)
            & (schema.source_document.c.sha256 == sha256)
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()

    def get_document_status(self, context: TenantContext, sha256: str) -> str | None:
        stmt = select(schema.source_document.c.parse_status).where(
            (schema.source_document.c.tenant_id == context.tenant_id)
            & (schema.source_document.c.sha256 == sha256)
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).scalar_one_or_none()

    def find_by_statement_key(self, context: TenantContext, statement_key: str) -> dict | None:
        columns = schema.source_document.c
        stmt = select(
            columns.id, columns.sha256, columns.source_relpath, columns.parse_status,
            columns.period_start, columns.period_end,
        ).where(
            (columns.tenant_id == context.tenant_id) & (columns.statement_key == statement_key)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        return dict(row._mapping) if row is not None else None

    def dedupe_keys_for_document(self, context: TenantContext, document_id: int) -> set[str]:
        stmt = select(schema.txn.c.dedupe_key).where(
            (schema.txn.c.tenant_id == context.tenant_id)
            & (schema.txn.c.source_document_id == document_id)
        )
        with self._engine.connect() as conn:
            return set(conn.execute(stmt).scalars())

    def list_documents(self, context: TenantContext, parse_status: str | None = None) -> list[dict]:
        columns = schema.source_document.c
        stmt = select(
            columns.sha256, columns.institution, columns.doc_type, columns.parse_status,
            columns.storage_path, columns.source_relpath, columns.source_profile,
            columns.layout_fingerprint,
        ).where(columns.tenant_id == context.tenant_id)
        if parse_status is not None:
            stmt = stmt.where(columns.parse_status == parse_status)
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt.order_by(columns.source_relpath))]

    def list_transfer_legs(self, context: TenantContext) -> list[dict]:
        """Every transaction, with the account it belongs to, for matching.

        The account reference comes along because it is evidence: DBS writes
        the far account into the row, so one leg naming the other's number is
        what turns a deduction into a reading.
        """
        txn, account = schema.txn.c, schema.account.c
        stmt = (
            select(
                txn.id, txn.account_id, txn.posted_date, txn.amount_minor,
                txn.description_raw, account.account_ref_masked,
            )
            .select_from(schema.txn.join(schema.account, txn.account_id == account.id))
            .where(txn.tenant_id == context.tenant_id)
            .order_by(txn.posted_date, txn.id)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def seed_categories(self, context: TenantContext, names) -> int:
        """Give a tenant the default taxonomy, once.

        Idempotent and additive: a category the tenant already has is left
        alone, including one they renamed. Re-seeding must never undo an
        operator's edits to their own taxonomy.
        """
        with self._engine.begin() as conn:
            existing = {
                row[0] for row in conn.execute(
                    select(schema.category.c.name)
                    .where(schema.category.c.tenant_id == context.tenant_id)
                )
            }
            missing = [
                {"tenant_id": context.tenant_id, "name": name, "position": index}
                for index, name in enumerate(names)
                if name not in existing
            ]
            if missing:
                conn.execute(schema.category.insert(), missing)
        return len(missing)

    def list_categories(self, context: TenantContext) -> list[dict]:
        columns = schema.category.c
        with self._engine.connect() as conn:
            return [
                dict(row._mapping) for row in conn.execute(
                    select(columns.id, columns.name, columns.position)
                    .where(columns.tenant_id == context.tenant_id)
                    .order_by(columns.position, columns.name)
                )
            ]

    def add_category_rules(self, context: TenantContext, rules) -> int:
        """Add rules, leaving any the tenant already has untouched.

        Additive by design. A rule the operator wrote or corrected outranks
        anything an import proposes, so an import that overwrote would undo
        exactly the work worth keeping.
        """
        by_name = {row["name"]: row["id"] for row in self.list_categories(context)}
        existing = {
            (row["pattern"], row["category"]) for row in self.list_category_rules(context)
        }
        new = [
            {
                "tenant_id": context.tenant_id,
                "category_id": by_name[category],
                "pattern": pattern,
                "weight": weight,
                "note": note,
                "created_at": datetime.now(timezone.utc),
            }
            for pattern, category, weight, note in rules
            if category in by_name and (pattern, category) not in existing
        ]
        if new:
            with self._engine.begin() as conn:
                conn.execute(schema.category_rule.insert(), new)
        return len(new)

    def add_category(self, context: TenantContext, name: str) -> int:
        """Add one category to the tenant's taxonomy, at the end."""
        existing = self.list_categories(context)
        with self._engine.begin() as conn:
            return conn.execute(schema.category.insert().values(
                tenant_id=context.tenant_id, name=name, position=len(existing),
            )).inserted_primary_key[0]

    def set_human_category(self, context: TenantContext, txn_id: int, category: str) -> None:
        """Record a correction the operator made to one transaction.

        Replaces every enrichment for that row, whatever produced it: a
        correction is the last word, and leaving the rule's opinion beside it
        would mean two answers with nothing to say which is current.
        """
        enrichment = schema.txn_enrichment.c
        with self._engine.begin() as conn:
            conn.execute(schema.txn_enrichment.delete().where(
                (enrichment.tenant_id == context.tenant_id) & (enrichment.txn_id == txn_id)
            ))
            conn.execute(schema.txn_enrichment.insert(), [{
                "tenant_id": context.tenant_id,
                "txn_id": txn_id,
                "category": category,
                "confidence": 1.0,
                "source": "human",
                "computed_at": datetime.now(timezone.utc),
            }])

    def clear_human_category(self, context: TenantContext, txn_id: int) -> bool:
        """Drop a correction, so the next rule pass decides the row again."""
        enrichment = schema.txn_enrichment.c
        with self._engine.begin() as conn:
            removed = conn.execute(schema.txn_enrichment.delete().where(
                (enrichment.tenant_id == context.tenant_id)
                & (enrichment.txn_id == txn_id)
                & (enrichment.source == "human")
            )).rowcount
        return bool(removed)

    def list_category_rules(self, context: TenantContext) -> list[dict]:
        """Rules with the category they resolve to, by id rather than by name.

        Joined here so a rename cannot leave a rule pointing at a category that
        no longer answers to that name.
        """
        rule, cat = schema.category_rule.c, schema.category.c
        stmt = (
            select(rule.id, rule.pattern, rule.weight, rule.note, cat.name.label("category"))
            .select_from(schema.category_rule.join(schema.category, rule.category_id == cat.id))
            .where(rule.tenant_id == context.tenant_id)
            .order_by(rule.weight.desc(), rule.id)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def replace_rule_enrichments(self, context: TenantContext, decided) -> dict:
        """Write what the rules decided, and touch nothing a human decided.

        `decided` is `(txn_id, category, confidence)`.

        Rule rows are replaced wholesale because they are a pure function of
        the rules and the ledger: rerunning must give the same answer, and
        appending would leave a transaction wearing two categories with nothing
        to say which is current.

        **Human corrections are never in scope.** They are deleted by no query
        here and overwritten by no insert, because the whole value of correcting
        something is that it stays corrected â€” an automated pass that could undo
        it would make every correction provisional.
        """
        enrichment = schema.txn_enrichment.c
        rows = [
            {
                "tenant_id": context.tenant_id,
                "txn_id": txn_id,
                "category": category,
                "confidence": confidence,
                "source": "rule",
                "computed_at": datetime.now(timezone.utc),
            }
            for txn_id, category, confidence in decided
        ]
        with self._engine.begin() as conn:
            human = conn.execute(
                select(func.count()).select_from(schema.txn_enrichment)
                .where((enrichment.tenant_id == context.tenant_id) & (enrichment.source == "human"))
            ).scalar_one()
            removed = conn.execute(
                schema.txn_enrichment.delete().where(
                    (enrichment.tenant_id == context.tenant_id) & (enrichment.source == "rule")
                )
            ).rowcount
            # A human decision stands, so the rule pass does not get to speak
            # about that transaction at all.
            if human:
                claimed = select(enrichment.txn_id).where(
                    (enrichment.tenant_id == context.tenant_id) & (enrichment.source == "human")
                )
                spoken_for = {row[0] for row in conn.execute(claimed)}
                rows = [r for r in rows if r["txn_id"] not in spoken_for]
            if rows:
                conn.execute(schema.txn_enrichment.insert(), rows)

        return {"written": len(rows), "replaced": removed or 0, "left_to_humans": human}

    def _spending_base(self, context: TenantContext, *, exclude_txn_ids=None, direction="out"):
        """The predicate every spending figure shares.

        One place, because a total, a trend bucket and a transaction list that
        disagree about what counts are worse than any of them being wrong on
        its own â€” the screen shows them side by side.

        Excluded: transfers between the household's own accounts, rows the
        operator has hidden, and anything the caller is hiding for this request
        only. The last of those is what makes session-level hiding change the
        averages rather than just the list.
        """
        txn, link, hidden = schema.txn.c, schema.transfer_link.c, schema.hidden_txn.c
        # `in_txn_id` is nullable for a one-sided manual link, and a NULL inside
        # a NOT IN makes the whole predicate NULL — which excludes every row
        # rather than none. Marking a single transfer once emptied the entire
        # dashboard this way, silently and to zero.
        linked = select(link.out_txn_id).where(link.tenant_id == context.tenant_id).union(
            select(link.in_txn_id).where(
                (link.tenant_id == context.tenant_id) & link.in_txn_id.isnot(None)
            )
        )
        put_away = select(hidden.txn_id).where(hidden.tenant_id == context.tenant_id)

        where = (
            (txn.tenant_id == context.tenant_id)
            & txn.id.notin_(linked)
            & txn.id.notin_(put_away)
        )
        # Direction is a parameter, not a constant, because the household wants
        # to know what it earned as well as what it spent — and the net of the
        # two, which is the only one of the three that is robust to a refund
        # being counted as income rather than as negative spending.
        if direction == "out":
            where = where & (txn.amount_minor < 0)
        elif direction == "in":
            where = where & (txn.amount_minor > 0)
        if exclude_txn_ids:
            where = where & txn.id.notin_(list(exclude_txn_ids))
        return where

    def list_hidden(self, context: TenantContext) -> list[dict]:
        """What has been put away, and what it comes to.

        Returned with the amounts so the operator can see the size of what
        they have excluded. A hidden total nobody can see is how a dashboard
        starts lying quietly.
        """
        hidden, txn, account = schema.hidden_txn.c, schema.txn.c, schema.account.c
        stmt = (
            select(
                txn.id, txn.posted_date, txn.amount_minor, txn.counterparty_norm,
                account.institution, hidden.note, hidden.hidden_at,
            )
            .select_from(
                schema.hidden_txn
                .join(schema.txn, hidden.txn_id == txn.id)
                .join(schema.account, txn.account_id == account.id)
            )
            .where(hidden.tenant_id == context.tenant_id)
            .order_by(txn.posted_date.desc())
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def hide_txn(self, context: TenantContext, txn_id: int, note: str = "") -> bool:
        hidden = schema.hidden_txn.c
        with self._engine.begin() as conn:
            already = conn.execute(
                select(hidden.id).where(
                    (hidden.tenant_id == context.tenant_id) & (hidden.txn_id == txn_id)
                )
            ).first()
            if already:
                return False
            conn.execute(schema.hidden_txn.insert(), [{
                "tenant_id": context.tenant_id, "txn_id": txn_id,
                "note": note, "hidden_at": datetime.now(timezone.utc),
            }])
        return True

    def unhide_txn(self, context: TenantContext, txn_id: int) -> bool:
        hidden = schema.hidden_txn.c
        with self._engine.begin() as conn:
            removed = conn.execute(
                schema.hidden_txn.delete().where(
                    (hidden.tenant_id == context.tenant_id) & (hidden.txn_id == txn_id)
                )
            ).rowcount
        return bool(removed)

    def spending_trend(
        self, context: TenantContext, *, bucket: str, since=None, until=None,
        account_ids=None, categories=None, exclude_txn_ids=None, rolling: int = 0,
    ) -> list[dict]:
        """Spending totalled per period.

        Bucketed in Python rather than in SQL: date truncation is the most
        dialect-specific thing either engine does, and Rule 1's seam is worth
        more than the milliseconds. The volume here is a household's ledger,
        not a warehouse.
        """
        txn = schema.txn.c
        stmt = (
            select(txn.posted_date, txn.amount_minor)
            # Both directions in one pass: a bucket needs what went out, what
            # came in, and the net, and three queries would be three chances
            # for the filters to drift apart.
            .where(self._spending_base(
                context, exclude_txn_ids=exclude_txn_ids, direction="net",
            ))
        )
        if since is not None:
            stmt = stmt.where(txn.posted_date >= since)
        if until is not None:
            stmt = stmt.where(txn.posted_date <= until)
        if account_ids:
            stmt = stmt.where(txn.account_id.in_(list(account_ids)))
        if categories:
            enrichment = schema.txn_enrichment.c
            stmt = stmt.where(txn.id.in_(
                select(enrichment.txn_id).where(
                    (enrichment.tenant_id == context.tenant_id)
                    & (enrichment.category.in_(list(categories)))
                )
            ))

        with self._engine.connect() as conn:
            rows = [(r[0], r[1]) for r in conn.execute(stmt)]
        return _rolling(_bucket(rows, bucket), rolling or rolling_window(bucket))

    def balance_history(self, context: TenantContext, *, since=None) -> list[dict]:
        """Every closing balance a statement declared, with the date it closed.

        Net worth is balance over time, and this is where balances live. It is
        deliberately not a sum of transactions: a movement between the
        household's own accounts would then read as growth on one side and loss
        on the other, and a card balance would count the wrong way round.

        Card balances are already stored negated, so a debt reduces the total
        without anything here needing to know which is which.
        """
        balance, document, account = (
            schema.statement_balance.c, schema.source_document.c, schema.account.c,
        )
        stmt = (
            select(
                document.period_end,
                balance.account_id,
                account.institution,
                account.account_ref_masked,
                account.sub_account_label,
                account.kind,
                balance.closing_balance_minor,
            )
            .select_from(
                schema.statement_balance
                .join(schema.source_document, balance.source_document_id == document.id)
                .join(schema.account, balance.account_id == account.id)
            )
            .where(
                (balance.tenant_id == context.tenant_id)
                & (balance.closing_balance_minor.isnot(None))
                & (document.period_end.isnot(None))
            )
            .order_by(document.period_end)
        )
        if since is not None:
            stmt = stmt.where(document.period_end >= since)
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def list_accounts(self, context: TenantContext) -> list[dict]:
        columns = schema.account.c
        stmt = (
            select(
                columns.id, columns.institution, columns.account_ref_masked,
                columns.sub_account_label, columns.currency, columns.kind,
            )
            .where(columns.tenant_id == context.tenant_id)
            .order_by(columns.institution, columns.account_ref_masked)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def list_spending(
        self,
        context: TenantContext,
        *,
        since=None,
        until=None,
        account_ids=None,
        categories=None,
        exclude_txn_ids=None,
        direction: str = "out",
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        """Spending rows with their category, filtered as the dashboard asks.

        Every figure the dashboard shows has to be derivable from a date range,
        a set of accounts and a set of categories â€” see architecture Â§5.1(B) â€”
        so this is one query with optional narrowing rather than a family of
        precomputed aggregates.

        Transfers are excluded here, not by the caller. A movement between the
        household's own accounts is not spending, and a total that includes it
        overstates by the size of every card payment.
        """
        txn, enrichment, account = (
            schema.txn.c, schema.txn_enrichment.c, schema.account.c,
        )
        stmt = (
            select(
                txn.id, txn.posted_date, txn.amount_minor, txn.currency,
                txn.counterparty_norm, txn.account_id,
                account.institution, account.account_ref_masked,
                enrichment.category, enrichment.source,
            )
            .select_from(
                schema.txn
                .join(schema.account, txn.account_id == account.id)
                .outerjoin(
                    schema.txn_enrichment,
                    (enrichment.txn_id == txn.id)
                    & (enrichment.tenant_id == context.tenant_id),
                )
            )
            .where(self._spending_base(
                context, exclude_txn_ids=exclude_txn_ids, direction=direction,
            ))
            .order_by(txn.posted_date.desc(), txn.id.desc())
        )
        if since is not None:
            stmt = stmt.where(txn.posted_date >= since)
        if until is not None:
            stmt = stmt.where(txn.posted_date <= until)
        if account_ids:
            stmt = stmt.where(txn.account_id.in_(list(account_ids)))
        if categories:
            stmt = stmt.where(enrichment.category.in_(list(categories)))

        with self._engine.connect() as conn:
            rows = conn.execute(stmt.limit(limit).offset(offset))
            return [dict(row._mapping) for row in rows]

    def spending_summary(
        self, context: TenantContext, *, since=None, until=None,
        account_ids=None, categories=None, exclude_txn_ids=None, direction="out",
    ) -> list[dict]:
        """Totals per category over the same filters, for the headline figures."""
        txn, enrichment = schema.txn.c, schema.txn_enrichment.c
        stmt = (
            select(
                enrichment.category,
                func.count().label("rows"),
                sum_minor(txn.amount_minor).label("total_minor"),
            )
            .select_from(
                schema.txn.outerjoin(
                    schema.txn_enrichment,
                    (enrichment.txn_id == txn.id)
                    & (enrichment.tenant_id == context.tenant_id),
                )
            )
            .where(self._spending_base(
                context, exclude_txn_ids=exclude_txn_ids, direction=direction,
            ))
            .group_by(enrichment.category)
        )
        if since is not None:
            stmt = stmt.where(txn.posted_date >= since)
        if until is not None:
            stmt = stmt.where(txn.posted_date <= until)
        if account_ids:
            stmt = stmt.where(txn.account_id.in_(list(account_ids)))
        if categories:
            stmt = stmt.where(enrichment.category.in_(list(categories)))

        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def category_totals(self, context: TenantContext) -> list[dict]:
        """What each category holds, for reading back what a pass achieved."""
        enrichment, txn = schema.txn_enrichment.c, schema.txn.c
        stmt = (
            select(
                enrichment.category,
                func.count().label("rows"),
                sum_minor(txn.amount_minor).label("total_minor"),
            )
            .select_from(
                schema.txn_enrichment.join(schema.txn, enrichment.txn_id == txn.id)
            )
            .where(enrichment.tenant_id == context.tenant_id)
            .group_by(enrichment.category)
            .order_by(sum_minor(txn.amount_minor))
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def list_categorisation_targets(self, context: TenantContext) -> list[dict]:
        """Spending rows needing a category, and what they were with.

        Transfers are excluded for the same reason recurrence excludes them:
        moving money between one's own accounts is not spending, so asking what
        it was *for* has no answer worth recording.
        """
        txn, link = schema.txn.c, schema.transfer_link.c
        linked = select(link.out_txn_id).where(link.tenant_id == context.tenant_id).union(
            # NULL-safe: a one-sided manual link carries no in_txn_id, and a
            # NULL inside NOT IN excludes every row instead of none.
            select(link.in_txn_id).where(
                (link.tenant_id == context.tenant_id) & link.in_txn_id.isnot(None)
            )
        )
        stmt = (
            select(txn.id, txn.counterparty_norm, txn.amount_minor, txn.posted_date)
            .where(
                (txn.tenant_id == context.tenant_id)
                & (txn.amount_minor < 0)
                & txn.id.notin_(linked)
            )
            .order_by(txn.posted_date, txn.id)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def list_recurrence_candidates(self, context: TenantContext) -> list[dict]:
        """Spending rows that could belong to a series.

        Transfers are excluded here rather than downstream. A credit-card
        payment repeats monthly against the same counterparty and would be read
        as a subscription while being neither spending nor a merchant â€” and it
        is the single most regular thing in the ledger, so it would be found
        first and trusted most.
        """
        txn, link = schema.txn.c, schema.transfer_link.c
        linked = select(link.out_txn_id).where(link.tenant_id == context.tenant_id).union(
            # NULL-safe: a one-sided manual link carries no in_txn_id, and a
            # NULL inside NOT IN excludes every row instead of none.
            select(link.in_txn_id).where(
                (link.tenant_id == context.tenant_id) & link.in_txn_id.isnot(None)
            )
        )
        stmt = (
            # counterparty_norm, not description_norm: the first is *who* the
            # row was with, the second keeps everything because it feeds the
            # dedupe key. Grouping on the latter makes one merchant paid three
            # ways look like three merchants, and nothing recurs.
            select(txn.id, txn.posted_date, txn.amount_minor, txn.counterparty_norm)
            .where(
                (txn.tenant_id == context.tenant_id)
                # Money out only: a series is something paid, and an incoming
                # salary is regular without being a subscription.
                & (txn.amount_minor < 0)
                & txn.id.notin_(linked)
            )
            .order_by(txn.posted_date, txn.id)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def mark_transfer(
        self, context: TenantContext, txn_id: int, counterpart_id: int | None = None,
    ) -> bool:
        """Record the operator's own claim that a row is a transfer.

        Kept apart from the matcher's links by `origin`, because a re-run
        rebuilds what the matcher found and must not erase what a person
        decided — the same rule corrections follow.
        """
        link = schema.transfer_link.c
        with self._engine.begin() as conn:
            already = conn.execute(
                select(link.id).where(
                    (link.tenant_id == context.tenant_id)
                    & ((link.out_txn_id == txn_id) | (link.in_txn_id == txn_id))
                )
            ).first()
            if already:
                return False
            amount = conn.execute(
                select(schema.txn.c.amount_minor).where(
                    (schema.txn.c.tenant_id == context.tenant_id)
                    & (schema.txn.c.id == txn_id)
                )
            ).scalar_one_or_none()
            if amount is None:
                return False
            conn.execute(schema.transfer_link.insert(), [{
                "tenant_id": context.tenant_id,
                "out_txn_id": txn_id,
                "in_txn_id": counterpart_id,
                "amount_minor": abs(amount),
                "days_apart": 0,
                "evidence": "marked by operator",
                "origin": "manual",
                "linked_at": datetime.now(timezone.utc),
            }])
        return True

    def unmark_transfer(self, context: TenantContext, txn_id: int) -> bool:
        link = schema.transfer_link.c
        with self._engine.begin() as conn:
            removed = conn.execute(
                schema.transfer_link.delete().where(
                    (link.tenant_id == context.tenant_id)
                    & (link.origin == "manual")
                    & ((link.out_txn_id == txn_id) | (link.in_txn_id == txn_id))
                )
            ).rowcount
        return bool(removed)

    def replace_transfer_links(self, context: TenantContext, links) -> int:
        """Rebuild this tenant's links from scratch, atomically.

        Replace rather than add: the pass is a pure function of the ledger, so
        running it twice must leave the same result. Appending would pair rows
        already paired and quietly double what is excluded from spending â€”
        an error that flatters the total, so nobody would go looking for it.
        """
        rows = [
            {
                "tenant_id": context.tenant_id,
                "out_txn_id": link.out_txn_id,
                "in_txn_id": link.in_txn_id,
                "amount_minor": link.amount_minor,
                "days_apart": link.days_apart,
                "evidence": link.evidence,
                "linked_at": datetime.now(timezone.utc),
            }
            for link in links
        ]
        with self._engine.begin() as conn:
            # Only what the matcher produced. A manual link is the operator's
            # claim and is not regenerable, so a re-run must leave it alone —
            # the same guarantee corrections have.
            conn.execute(
                schema.transfer_link.delete().where(
                    (schema.transfer_link.c.tenant_id == context.tenant_id)
                    & (schema.transfer_link.c.origin == "auto")
                )
            )
            if rows:
                conn.execute(schema.transfer_link.insert(), rows)
        return len(rows)

    def count_transfer_links(self, context: TenantContext) -> int:
        with self._engine.connect() as conn:
            return conn.execute(
                select(func.count()).select_from(schema.transfer_link)
                .where(schema.transfer_link.c.tenant_id == context.tenant_id)
            ).scalar_one()

    def delete_document(self, context: TenantContext, sha256: str) -> bool:
        """Remove a document and everything derived from it, atomically.

        The stored original is deliberately left alone: it is immutable and is
        exactly what a reparse reads from.
        """
        with self._engine.begin() as conn:
            document_id = conn.execute(
                select(schema.source_document.c.id).where(
                    (schema.source_document.c.tenant_id == context.tenant_id)
                    & (schema.source_document.c.sha256 == sha256)
                )
            ).scalar_one_or_none()
            if document_id is None:
                return False

            # Links first: they point at these rows, and a transfer link is a
            # claim about a ledger that is about to change. Rebuilding it is a
            # single command, so dropping it costs nothing and keeping it would
            # block the delete on a foreign key.
            doomed = select(schema.txn.c.id).where(
                (schema.txn.c.tenant_id == context.tenant_id)
                & (schema.txn.c.source_document_id == document_id)
            )
            conn.execute(
                schema.transfer_link.delete().where(
                    (schema.transfer_link.c.tenant_id == context.tenant_id)
                    & (
                        schema.transfer_link.c.out_txn_id.in_(doomed)
                        | schema.transfer_link.c.in_txn_id.in_(doomed)
                    )
                )
            )
            conn.execute(
                schema.txn.delete().where(
                    (schema.txn.c.tenant_id == context.tenant_id)
                    & (schema.txn.c.source_document_id == document_id)
                )
            )
            conn.execute(
                schema.statement_balance.delete().where(
                    (schema.statement_balance.c.tenant_id == context.tenant_id)
                    & (schema.statement_balance.c.source_document_id == document_id)
                )
            )
            conn.execute(
                schema.source_document.delete().where(schema.source_document.c.id == document_id)
            )
        return True

    def existing_dedupe_keys(self, context: TenantContext, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        found: set[str] = set()
        with self._engine.connect() as conn:
            for start in range(0, len(keys), _KEY_CHUNK):
                chunk = keys[start:start + _KEY_CHUNK]
                stmt = select(schema.txn.c.dedupe_key).where(
                    (schema.txn.c.tenant_id == context.tenant_id)
                    & (schema.txn.c.dedupe_key.in_(chunk))
                )
                found.update(conn.execute(stmt).scalars())
        return found

    def counts(self, context: TenantContext) -> StatusCounts:
        tenant = context.tenant_id
        with self._engine.connect() as conn:
            by_status = dict(
                conn.execute(
                    select(schema.source_document.c.parse_status, func.count())
                    .where(schema.source_document.c.tenant_id == tenant)
                    .group_by(schema.source_document.c.parse_status)
                ).all()
            )
            by_institution = dict(
                conn.execute(
                    select(schema.source_document.c.institution, func.count())
                    .where(
                        (schema.source_document.c.tenant_id == tenant)
                        & (schema.source_document.c.parse_status != "quarantined")
                    )
                    .group_by(schema.source_document.c.institution)
                ).all()
            )
            quarantined_by_institution = dict(
                conn.execute(
                    select(schema.source_document.c.institution, func.count())
                    .where(
                        (schema.source_document.c.tenant_id == tenant)
                        & (schema.source_document.c.parse_status == "quarantined")
                    )
                    .group_by(schema.source_document.c.institution)
                ).all()
            )
            accounts = conn.execute(
                select(func.count()).select_from(schema.account)
                .where(schema.account.c.tenant_id == tenant)
            ).scalar_one()
            txns = conn.execute(
                select(func.count()).select_from(schema.txn)
                .where(schema.txn.c.tenant_id == tenant)
            ).scalar_one()
        return StatusCounts(
            documents=by_status.get("imported", 0) + by_status.get("imported_unverified", 0),
            documents_unverified=by_status.get("imported_unverified", 0),
            documents_quarantined=by_status.get("quarantined", 0),
            accounts=accounts,
            txns=txns,
            by_institution=by_institution,
            quarantined_by_institution=quarantined_by_institution,
        )

    # -- writes --------------------------------------------------------------

    def insert_document(
        self,
        context: TenantContext,
        document: DocumentRecord,
        balances: list[BalanceRecord],
        txns: list[TxnRecord],
    ) -> InsertResult:
        """Persist a document and everything it carries, atomically.

        One transaction covers the document row, every account upsert, the
        per-account balances and all transactions. A failure anywhere rolls
        back the whole document â€” there is no such thing as a half-imported
        statement.
        """
        fetched_at = document.fetched_at or datetime.now(timezone.utc)
        tenant = context.tenant_id

        with self._engine.begin() as conn:
            existing = conn.execute(
                select(schema.source_document.c.id).where(
                    (schema.source_document.c.tenant_id == tenant)
                    & (schema.source_document.c.sha256 == document.sha256)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return InsertResult(document_id=existing)

            document_id = conn.execute(
                schema.source_document.insert().values(
                    tenant_id=tenant,
                    uploaded_by_member_id=context.member_id,
                    sha256=document.sha256,
                    statement_key=document.statement_key,
                    institution=document.institution,
                    doc_type=document.doc_type,
                    period_start=document.period_start,
                    period_end=document.period_end,
                    statement_date=document.statement_date,
                    fetched_at=fetched_at,
                    fetch_method=document.fetch_method,
                    storage_path=document.storage_path,
                    parser_version=document.parser_version,
                    layout_fingerprint=document.layout_fingerprint,
                    parse_status=document.parse_status,
                    source_profile=document.source_profile,
                    source_relpath=document.source_relpath,
                )
            ).inserted_primary_key[0]

            account_ids: dict[tuple, int] = {}
            for record in balances:
                account_ids[_account_key(record.account_key)] = self._upsert_account(conn, tenant, record.account_key)
            for record in txns:
                key = _account_key(record.account_key)
                if key not in account_ids:
                    account_ids[key] = self._upsert_account(conn, tenant, record.account_key)

            for record in balances:
                conn.execute(
                    schema.statement_balance.insert().values(
                        tenant_id=tenant,
                        account_id=account_ids[_account_key(record.account_key)],
                        source_document_id=document_id,
                        opening_balance_minor=record.opening_balance_minor,
                        closing_balance_minor=record.closing_balance_minor,
                    )
                )

            inserted, skipped = self._insert_txns(conn, tenant, document_id, account_ids, txns)

        return InsertResult(
            document_id=document_id,
            accounts=len(account_ids),
            txns_inserted=inserted,
            txns_skipped=skipped,
        )

    def _upsert_account(self, conn, tenant_id: int, record: AccountRecord) -> int:
        where = (
            (schema.account.c.tenant_id == tenant_id)
            & (schema.account.c.institution == record.institution)
            & (schema.account.c.account_ref_masked == record.account_ref_masked)
            & (schema.account.c.sub_account_label == (record.sub_account_label or ""))
            & (schema.account.c.currency == record.currency)
        )
        found = conn.execute(select(schema.account.c.id).where(where)).scalar_one_or_none()
        if found is not None:
            return found
        return conn.execute(
            schema.account.insert().values(
                tenant_id=tenant_id,
                # NULL means shared across the tenant. Statement ingestion
                # cannot know whose account it is, and guessing would be worse
                # than leaving it joint until someone says otherwise.
                owner_member_id=None,
                institution=record.institution,
                account_ref_masked=record.account_ref_masked,
                sub_account_label=record.sub_account_label or "",
                currency=record.currency,
                kind=record.kind,
            )
        ).inserted_primary_key[0]

    def _insert_txns(self, conn, tenant_id: int, document_id: int, account_ids: dict, txns: list[TxnRecord]) -> tuple[int, int]:
        if not txns:
            return 0, 0

        keys = [t.dedupe_key for t in txns]
        already: set[str] = set()
        for start in range(0, len(keys), _KEY_CHUNK):
            chunk = keys[start:start + _KEY_CHUNK]
            already.update(
                conn.execute(
                    select(schema.txn.c.dedupe_key).where(
                        (schema.txn.c.tenant_id == tenant_id)
                        & (schema.txn.c.dedupe_key.in_(chunk))
                    )
                ).scalars()
            )

        rows = []
        seen_in_batch: set[str] = set()
        for t in txns:
            if t.dedupe_key in already or t.dedupe_key in seen_in_batch:
                continue
            seen_in_batch.add(t.dedupe_key)
            rows.append({
                "tenant_id": tenant_id,
                "account_id": account_ids[_account_key(t.account_key)],
                "source_document_id": document_id,
                "posted_date": t.posted_date,
                "value_date": t.value_date,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "fx_amount_minor": t.fx_amount_minor,
                "fx_currency": t.fx_currency,
                "fx_rate": t.fx_rate,
                "description_raw": t.description_raw,
                "description_norm": t.description_norm,
                "counterparty_norm": t.counterparty_norm,
                "seq": t.seq,
                "dedupe_key": t.dedupe_key,
            })

        skipped = len(txns) - len(rows)
        if not rows:
            return 0, skipped

        try:
            conn.execute(schema.txn.insert(), rows)
        except IntegrityError:
            # Backstop for a concurrent writer having inserted the same key
            # between the check above and this insert. Falling back to one row
            # at a time keeps the rest of the document importable.
            inserted = 0
            for row in rows:
                savepoint = conn.begin_nested()
                try:
                    conn.execute(schema.txn.insert(), [row])
                    savepoint.commit()
                    inserted += 1
                except IntegrityError:
                    savepoint.rollback()
                    skipped += 1
            return inserted, skipped

        return len(rows), skipped


def choose_bucket(days: int | None) -> str:
    """How wide a bar should be, given how much calendar is on screen.

    The operator's rule: days at a fortnight or less, weeks up to a year,
    months beyond it. Fixed bar counts were the alternative and are worse â€” a
    year of daily bars is unreadable and a fortnight of monthly ones is a
    single block.
    """
    if days is None:
        return "month"
    if days <= 14:
        return "day"
    if days < 365:
        return "week"
    return "month"


def _bucket_start(day, bucket: str):
    if bucket == "day":
        return day
    if bucket == "week":
        # Monday. A week a person recognises, rather than one counted back from
        # wherever the range happened to begin.
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def trend_centre(points: list[dict]) -> dict:
    """Where the middle of a run of buckets sits.

    Both the mean and the median are returned, and the gap between them is the
    useful part. A household's spending is not symmetric: one renovation or one
    insurance premium drags a mean somewhere no ordinary month has ever been,
    while the median keeps describing a typical period. When the two are far
    apart, the average is being carried by a handful of large one-offs — which
    is worth showing rather than resolving by picking a favourite.

    A reference line on a chart should use the median for that reason. The mean
    is kept because it is what multiplies back out to the total.
    """
    if not points:
        return {"mean_minor": None, "median_minor": None, "buckets": 0}

    values = sorted(p["total_minor"] for p in points)
    middle = len(values) // 2
    median = (
        values[middle] if len(values) % 2
        else round((values[middle - 1] + values[middle]) / 2)
    )
    return {
        "mean_minor": round(sum(values) / len(values)),
        "median_minor": median,
        "buckets": len(values),
    }


def rolling_window(bucket: str) -> int:
    """How many buckets a trailing average covers.

    Roughly a season in each case: a quarter of months, a month of weeks, a
    week of days. Short enough to still move, long enough that one large
    purchase does not make the line say "trending up".
    """
    return {"day": 7, "week": 4, "month": 3}.get(bucket, 3)


def _bucket(rows, bucket: str) -> list[dict]:
    """Out, in and net per period.

    All three from one pass. A household wants to know what it earned as well
    as what it spent, and the net is the only one of the three that survives a
    refund being counted as income rather than as negative spending — the two
    halves are each slightly wrong in ways that cancel.

    Bucketed in Python rather than SQL because date truncation is the most
    dialect-specific thing either engine does, and Rule 1's seam is worth more
    than the milliseconds at a household's volume.
    """
    out: dict = {}
    into: dict = {}
    counts: dict = {}
    for day, amount_minor in rows:
        start = _bucket_start(day, bucket)
        counts[start] = counts.get(start, 0) + 1
        if amount_minor < 0:
            out[start] = out.get(start, 0) + amount_minor
        else:
            into[start] = into.get(start, 0) + amount_minor

    periods = sorted(set(out) | set(into))
    return [
        {
            "period": start,
            "out_minor": out.get(start, 0),
            "in_minor": into.get(start, 0),
            "net_minor": out.get(start, 0) + into.get(start, 0),
            "rows": counts.get(start, 0),
            # Kept so existing callers reading a spending trend still work.
            "total_minor": out.get(start, 0),
        }
        for start in periods
    ]


def _rolling(points: list[dict], window: int) -> list[dict]:
    """A trailing average on each measure.

    Trailing rather than centred: a centred average needs periods that have not
    happened yet, and the question being asked is whether things are heading up
    *now*. Early points average over fewer buckets rather than being omitted,
    and `rolling_of` says how many, so a client can mark the stretch where the
    line is not yet on its full window.
    """
    result = []
    for index, point in enumerate(points):
        span = points[max(0, index - window + 1): index + 1]
        result.append({
            **point,
            "rolling": {
                "out_minor": round(sum(p["out_minor"] for p in span) / len(span)),
                "in_minor": round(sum(p["in_minor"] for p in span) / len(span)),
                "net_minor": round(sum(p["net_minor"] for p in span) / len(span)),
            },
            "rolling_of": len(span),
        })
    return result


def _account_key(record: AccountRecord) -> tuple:
    return (
        record.institution,
        record.account_ref_masked,
        record.sub_account_label or "",
        record.currency,
    )
