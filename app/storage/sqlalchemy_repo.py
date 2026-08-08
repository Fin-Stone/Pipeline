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

from ..domain.models import CARD
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


def _matches(q: str):
    """Free-text search over what a transaction says it was.

    Both columns, because they answer different questions. `counterparty_norm`
    is what the review queue and the rules key on, but normalisation strips
    mechanism words and references — so the operator searching for the text they
    remember seeing on the statement needs `description_raw` too.

    `icontains` with `autoescape` rather than a hand-built LIKE: a `%` typed
    into the search box is a percent sign the operator is looking for, not a
    wildcard that quietly matches the whole ledger. SQLAlchemy renders it as
    `lower(x) LIKE lower(y)` on SQLite and `ILIKE` on Postgres, so one
    expression serves both engines.
    """
    txn = schema.txn.c
    return (
        txn.counterparty_norm.icontains(q, autoescape=True)
        | txn.description_raw.icontains(q, autoescape=True)
    )


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
        """The tenant and member every request runs as.

        Read first, create second. The ensure_* path costs three round trips —
        find or make the tenant, find or make the member, then read the role —
        and every API request paid all three to discover that nothing had
        changed since the last one. On an existing install the answer is one
        join, and creation is a first-run event.

        Not cached. A context is an identity, and a stale identity is the one
        kind of wrong a multi-tenant system cannot afford: `restore` renumbers
        tenants, and a process holding the old id would keep answering with
        somebody else's ledger. Nine milliseconds is not worth that.
        """
        if member_email:
            tenant, member = schema.tenant.c, schema.member.c
            found = select(tenant.id, member.id.label("member_id"), member.role).select_from(
                schema.tenant.join(
                    schema.member,
                    (member.tenant_id == tenant.id) & (member.email == member_email),
                )
            ).where(tenant.slug == tenant_slug)
            with self._engine.connect() as conn:
                row = conn.execute(found).one_or_none()
            if row is not None:
                return TenantContext(
                    tenant_id=row.id, member_id=row.member_id, role=row.role
                )

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

        So does the account *kind*. Money leaving a deposit and landing on a
        card is a payment, and a payment is a transfer whatever the dates say —
        the purchases the card made are already counted as spending.

        And so do the card numbers the account has been known by. A card is
        keyed by its product because numbers change on reissue, but the deposit
        statement paying the bill names the number, so the numbers are what
        join the two.
        """
        txn, account, card = schema.txn.c, schema.account.c, schema.account_card_number.c
        stmt = (
            select(
                txn.id, txn.account_id, txn.posted_date, txn.amount_minor,
                txn.description_raw, account.account_ref_masked, account.kind,
            )
            .select_from(schema.txn.join(schema.account, txn.account_id == account.id))
            .where(txn.tenant_id == context.tenant_id)
            .order_by(txn.posted_date, txn.id)
        )
        # Grouped in Python rather than aggregated in SQL: string aggregation is
        # spelled differently on the two engines, and a handful of card numbers
        # is not worth a dialect branch. Rule 1.
        numbers: dict[int, list[str]] = {}
        with self._engine.connect() as conn:
            for row in conn.execute(
                select(card.account_id, card.card_number_masked)
                .where(card.tenant_id == context.tenant_id)
                .order_by(card.account_id, card.first_seen, card.card_number_masked)
            ):
                numbers.setdefault(row.account_id, []).append(row.card_number_masked)
            return [
                dict(row._mapping, card_numbers=tuple(numbers.get(row.account_id, ())))
                for row in conn.execute(stmt)
            ]

    def get_settings(self, context: TenantContext) -> dict[str, str]:
        """Every choice this tenant has made. Absent means "use the default"."""
        setting = schema.tenant_setting.c
        stmt = select(setting.key, setting.value).where(
            setting.tenant_id == context.tenant_id
        )
        with self._engine.connect() as conn:
            return {row.key: row.value for row in conn.execute(stmt)}

    def set_settings(self, context: TenantContext, values: dict[str, str]) -> int:
        """Store choices, replacing any already made. `None` removes one.

        Removing rather than storing a default is deliberate: an absent setting
        means "whatever the code thinks best today", and a stored copy of
        today's default would freeze this install at it forever.
        """
        setting = schema.tenant_setting.c
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            for key, value in values.items():
                conn.execute(schema.tenant_setting.delete().where(
                    (setting.tenant_id == context.tenant_id) & (setting.key == key)
                ))
                if value is not None:
                    conn.execute(schema.tenant_setting.insert(), [{
                        "tenant_id": context.tenant_id,
                        "key": key,
                        "value": str(value),
                        "updated_at": now,
                    }])
        return len(values)

    def list_transfer_links(self, context: TenantContext) -> list[dict]:
        """Every link, manual and automatic, as pairs of ids.

        Used to say what a re-run would *change* rather than only what it would
        find. "206 links" tells an operator nothing about whether to apply it;
        "9 new, 2 gone" is the whole decision.
        """
        link = schema.transfer_link.c
        stmt = select(
            link.out_txn_id, link.in_txn_id, link.amount_minor, link.origin,
        ).where(link.tenant_id == context.tenant_id)
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
            select(
                rule.id, rule.pattern, rule.weight, rule.note, rule.created_at,
                cat.name.label("category"),
            )
            .select_from(schema.category_rule.join(schema.category, rule.category_id == cat.id))
            .where(rule.tenant_id == context.tenant_id)
            .order_by(rule.weight.desc(), rule.id)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def delete_category_rule(self, context: TenantContext, rule_id: int) -> bool:
        """Remove one rule — the way back from a decision.

        `False` for a rule that is not there, so deleting twice is the same as
        deleting once: an undo that errors on a second click is worse than one
        that does nothing.

        Nothing else is touched. A rule decides what the *next* categorisation
        pass writes, exactly as adding one does, so removing it is symmetric
        with `add_category_rules` rather than a quiet rewrite of the ledger.
        The enrichments the rule already produced carry `source: rule` and are
        replaced wholesale by that pass — see `replace_rule_enrichments`.
        """
        rule = schema.category_rule.c
        with self._engine.begin() as conn:
            removed = conn.execute(schema.category_rule.delete().where(
                (rule.tenant_id == context.tenant_id) & (rule.id == rule_id)
            )).rowcount
        return bool(removed)

    def counterparty_row_counts(self, context: TenantContext, names) -> dict[str, int]:
        """How many rows each of `names` accounts for.

        What makes removing a decision a considered act rather than a guess:
        "this covers 47 rows" is the difference between undoing a typo and
        undoing a month of work. Counted over every row, including ones already
        categorised, because a rule claims a name whatever else has been said
        about it.

        Compared exactly. `normalise_counterparty` upper-cases what it stores
        and `Rule.literal` upper-cases what it extracts, so both sides are
        already folded — and folding again in SQL would cost a full scan on
        every call to buy nothing.
        """
        wanted = [n for n in dict.fromkeys(names) if n]
        if not wanted:
            return {}
        txn = schema.txn.c
        stmt = (
            select(txn.counterparty_norm, func.count().label("rows"))
            .where((txn.tenant_id == context.tenant_id) & txn.counterparty_norm.in_(wanted))
            .group_by(txn.counterparty_norm)
        )
        with self._engine.connect() as conn:
            return {row.counterparty_norm: row.rows for row in conn.execute(stmt)}

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

        Excluded: transfers between the household's own accounts, inflows that
        settle a shared charge, rows the operator has hidden, and anything the
        caller is hiding for this request only. The last of those is what makes
        session-level hiding change the averages rather than just the list.

        Note what is *not* excluded: the shared charge itself. It was real
        spending, just less of it than the statement says, so it stays in and
        `_paid_back` shrinks it. Dropping it would erase the household's own
        share along with everyone else's.
        """
        txn, link, hidden = schema.txn.c, schema.transfer_link.c, schema.hidden_txn.c
        payback = schema.payback_link.c
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
        # Not income: somebody paying back their share of something already
        # counted. NOT NULL, so the trap above cannot bite here — but it is the
        # same shape, so it is written the same way.
        settled = select(payback.income_txn_id).where(payback.tenant_id == context.tenant_id)

        where = (
            (txn.tenant_id == context.tenant_id)
            & txn.id.notin_(linked)
            & txn.id.notin_(put_away)
            & txn.id.notin_(settled)
        )
        # Money arriving on a credit card is never income, and this is where
        # that is enforced rather than in each of the three figures.
        #
        # Card balances are stored negated, so a positive row on a card means
        # the debt went down — never that the household got richer. There are
        # only three ways that happens and none of them is earnings: paying the
        # bill from one's own account, a merchant refunding a purchase, and
        # cashback or points. The first is a transfer; the other two reverse
        # spending that is already counted. Left in, an unmatched bill payment
        # reads as a month's salary — one card payment of $2,000.00 did exactly
        # that here, because the statement it was paid from had not been
        # imported yet and nothing could pair it.
        #
        # Excluded from the total rather than subtracted from spending. A
        # refund belongs against the charge it reverses, and `payback_link` is
        # how a person says which charge that is; guessing here would move
        # money out of a category on the strength of a date.
        cards = select(schema.account.c.id).where(
            (schema.account.c.tenant_id == context.tenant_id)
            & (schema.account.c.kind == CARD)
        )
        # Direction is a parameter, not a constant, because the household wants
        # to know what it earned as well as what it spent — and the net of the
        # two. `net` carries the same exclusion, so the trend and the tiles
        # cannot disagree about what counts as income.
        if direction == "out":
            where = where & (txn.amount_minor < 0)
        elif direction == "in":
            where = where & (txn.amount_minor > 0) & txn.account_id.notin_(cards)
        elif direction == "net":
            where = where & ((txn.amount_minor < 0) | txn.account_id.notin_(cards))
        if exclude_txn_ids:
            where = where & txn.id.notin_(list(exclude_txn_ids))
        return where

    def _paid_back(self, context: TenantContext):
        """Per charge, how much has come back for it. A joinable subquery.

        Grouped once and outer-joined rather than correlated per row, so the
        three places that total money stay one pass each.

        Used by every figure that *sums* money and by none that ask what a
        transaction was. A $300 dinner is a Dining charge whoever ended up
        paying for it, so categorisation, recurrence and the review queue all
        keep reading the raw amount — see `list_categorisation_targets`.
        """
        payback = schema.payback_link.c
        return (
            select(
                payback.expense_txn_id.label("txn_id"),
                # `sum_minor`, not a bare SUM. Postgres widens SUM(bigint) to
                # numeric, and adding that to a bigint gives numeric all the way
                # out to a quoted JSON string — which is how a trend point's
                # out_minor stopped being a number the moment paybacks existed.
                sum_minor(payback.amount_minor).label("paid_back"),
            )
            .where(payback.tenant_id == context.tenant_id)
            .group_by(payback.expense_txn_id)
            .subquery("paid_back")
        )

    @staticmethod
    def _effective(paid_back):
        """A transaction's amount after anything that came back for it.

        The signs do the arithmetic: a -300.00 charge with +270.00 of paybacks
        is -30.00. COALESCE because the outer join gives NULL for the
        overwhelming majority of rows, which nobody has paid back anything for.
        """
        return schema.txn.c.amount_minor + func.coalesce(paid_back.c.paid_back, 0)

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
        account_ids=None, categories=None, exclude_txn_ids=None, q=None,
        rolling: int = 0,
    ) -> list[dict]:
        """Money totalled per period.

        Bucketed in Python rather than in SQL: date truncation is the most
        dialect-specific thing either engine does, and Rule 1's seam is worth
        more than the milliseconds. The volume here is a household's ledger,
        not a warehouse.
        """
        txn = schema.txn.c
        paid_back = self._paid_back(context)
        stmt = (
            select(txn.posted_date, self._effective(paid_back))
            .select_from(schema.txn.outerjoin(paid_back, paid_back.c.txn_id == txn.id))
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
        if q:
            stmt = stmt.where(_matches(q))
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

    def list_card_numbers(self, context: TenantContext) -> list[dict]:
        """Every card number on record, with the account it belongs to."""
        card, account = schema.account_card_number.c, schema.account.c
        stmt = (
            select(
                card.account_id, account.institution, account.account_ref_masked,
                card.card_number_masked, card.first_seen, card.last_seen,
            )
            .select_from(
                schema.account_card_number.join(
                    schema.account, card.account_id == account.id
                )
            )
            .where(card.tenant_id == context.tenant_id)
            .order_by(account.institution, account.account_ref_masked, card.first_seen)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def record_card_numbers(self, context: TenantContext, records, seen_on) -> int:
        """Record card numbers for accounts that already exist. Returns how many are new.

        Separate from `insert_document` so that a ledger built before the
        numbers were collected can gain them **without a reparse**. A reparse
        assigns new transaction ids, which silently discards every human
        categorisation, hidden row and manual transfer mark attached to the old
        ones — far too much to pay for a field that changes no figure on its
        own.

        Accounts are looked up, never created. A statement whose account is not
        in the ledger is not something this should invent one for.
        """
        wanted = [record for record in records if record.card_numbers]
        if not wanted:
            return 0

        card = schema.account_card_number.c
        with self._engine.begin() as conn:
            before = conn.execute(
                select(func.count()).select_from(schema.account_card_number)
                .where(card.tenant_id == context.tenant_id)
            ).scalar_one()

            account_ids: dict[tuple, int] = {}
            for record in wanted:
                found = conn.execute(
                    select(schema.account.c.id)
                    .where(_account_where(context.tenant_id, record))
                ).scalar_one_or_none()
                if found is not None:
                    account_ids[_account_key(record)] = found

            self._record_card_numbers(
                conn, context.tenant_id, account_ids,
                [r for r in wanted if _account_key(r) in account_ids], seen_on,
            )
            after = conn.execute(
                select(func.count()).select_from(schema.account_card_number)
                .where(card.tenant_id == context.tenant_id)
            ).scalar_one()
        return after - before

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
        q=None,
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
        paid_back = self._paid_back(context)
        stmt = (
            select(
                txn.id, txn.posted_date, txn.amount_minor, txn.currency,
                txn.counterparty_norm, txn.description_raw, txn.account_id,
                account.institution, account.account_ref_masked,
                enrichment.category, enrichment.source,
                # Both figures, never one. A client showing "-30.00" with no
                # sight of the -300.00 it came from cannot be audited, and a
                # client doing the subtraction itself is doing arithmetic on
                # money this API promised to do for it.
                func.coalesce(paid_back.c.paid_back, 0).label("paid_back_minor"),
                self._effective(paid_back).label("effective_amount_minor"),
            )
            .select_from(
                schema.txn
                .join(schema.account, txn.account_id == account.id)
                .outerjoin(
                    schema.txn_enrichment,
                    (enrichment.txn_id == txn.id)
                    & (enrichment.tenant_id == context.tenant_id),
                )
                .outerjoin(paid_back, paid_back.c.txn_id == txn.id)
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
        if q:
            stmt = stmt.where(_matches(q))
        if categories:
            stmt = stmt.where(enrichment.category.in_(list(categories)))

        with self._engine.connect() as conn:
            rows = conn.execute(stmt.limit(limit).offset(offset))
            return [dict(row._mapping) for row in rows]

    def spending_summary(
        self, context: TenantContext, *, since=None, until=None,
        account_ids=None, categories=None, exclude_txn_ids=None, direction="out",
        q=None,
    ) -> list[dict]:
        """Totals per category over the same filters, for the headline figures."""
        txn, enrichment = schema.txn.c, schema.txn_enrichment.c
        paid_back = self._paid_back(context)
        stmt = (
            select(
                enrichment.category,
                func.count().label("rows"),
                sum_minor(self._effective(paid_back)).label("total_minor"),
            )
            .select_from(
                schema.txn.outerjoin(
                    schema.txn_enrichment,
                    (enrichment.txn_id == txn.id)
                    & (enrichment.tenant_id == context.tenant_id),
                )
                .outerjoin(paid_back, paid_back.c.txn_id == txn.id)
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
        if q:
            stmt = stmt.where(_matches(q))
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

    # -- paybacks ------------------------------------------------------------

    def list_paybacks(self, context: TenantContext, expense_txn_id: int | None = None):
        """What has come back, and for which charge."""
        payback, txn, account = schema.payback_link.c, schema.txn.c, schema.account.c
        stmt = (
            select(
                payback.id, payback.expense_txn_id, payback.income_txn_id,
                payback.amount_minor, payback.note, payback.linked_at,
                txn.posted_date, txn.counterparty_norm, account.institution,
            )
            .select_from(
                schema.payback_link
                .join(schema.txn, payback.income_txn_id == txn.id)
                .join(schema.account, txn.account_id == account.id)
            )
            .where(payback.tenant_id == context.tenant_id)
            .order_by(txn.posted_date, payback.id)
        )
        if expense_txn_id is not None:
            stmt = stmt.where(payback.expense_txn_id == expense_txn_id)
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def list_payback_candidates(
        self, context: TenantContext, expense_txn_id: int, *,
        q=None, within_days: int | None = None, limit: int = 100,
    ) -> list[dict]:
        """Inflows that could be somebody settling this charge.

        Ordered by how far each sits from the charge's own date, because a
        payback usually follows within days and the operator is scanning for it
        rather than reading a ledger. Anything already spoken for — settling
        another charge, part of a transfer, hidden — is not offered: the link
        would be refused, and offering a choice that cannot be taken is worse
        than not offering it.
        """
        txn, account = schema.txn.c, schema.account.c
        with self._engine.connect() as conn:
            charge_date = conn.execute(
                select(txn.posted_date).where(
                    (txn.tenant_id == context.tenant_id) & (txn.id == expense_txn_id)
                )
            ).scalar_one_or_none()
        if charge_date is None:
            raise LookupError(f"no transaction {expense_txn_id} in this ledger")

        stmt = (
            select(
                txn.id, txn.posted_date, txn.amount_minor, txn.currency,
                txn.counterparty_norm, txn.description_raw,
                account.institution, account.account_ref_masked,
            )
            .select_from(schema.txn.join(schema.account, txn.account_id == account.id))
            .where(self._spending_base(context, direction="in"))
        )
        if q:
            stmt = stmt.where(_matches(q))
        if within_days is not None:
            stmt = stmt.where(
                txn.posted_date >= charge_date - timedelta(days=within_days)
            ).where(
                txn.posted_date <= charge_date + timedelta(days=within_days)
            )

        with self._engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        # Sorted in Python: "absolute days from the charge" is an expression
        # both engines spell differently, and this is a page of rows.
        rows.sort(key=lambda r: (abs((r["posted_date"] - charge_date).days), -r["id"]))
        return rows[:limit]

    def link_paybacks(
        self, context: TenantContext, expense_txn_id: int, income_txn_ids, note: str = "",
    ) -> dict:
        """Record that these inflows settle part of this charge.

        Every check runs inside the transaction that writes, so two clients
        linking the same inflow cannot both win. Refuses rather than clamps:
        a payback that silently did less than it said would leave the operator
        looking at a number they cannot explain, which is the failure this
        feature exists to remove.
        """
        txn, payback = schema.txn.c, schema.payback_link.c
        wanted = list(dict.fromkeys(income_txn_ids))  # de-duplicated, order kept
        if not wanted:
            raise ValueError("no paybacks given")

        with self._engine.begin() as conn:
            amounts = dict(conn.execute(
                select(txn.id, txn.amount_minor).where(
                    (txn.tenant_id == context.tenant_id)
                    & txn.id.in_([expense_txn_id, *wanted])
                )
            ).all())

            if expense_txn_id not in amounts:
                raise LookupError(f"no transaction {expense_txn_id} in this ledger")
            charge = amounts[expense_txn_id]
            if charge >= 0:
                raise ValueError(
                    f"transaction {expense_txn_id} is not a charge: it is money in, "
                    "and a payback settles money out"
                )

            missing = [i for i in wanted if i not in amounts]
            if missing:
                raise LookupError(f"no transaction {missing[0]} in this ledger")
            not_income = [i for i in wanted if amounts[i] <= 0]
            if not_income:
                raise ValueError(
                    f"transaction {not_income[0]} is not money in, so it cannot be a payback"
                )

            taken = conn.execute(
                select(payback.income_txn_id, payback.expense_txn_id).where(
                    (payback.tenant_id == context.tenant_id)
                    & payback.income_txn_id.in_(wanted)
                )
            ).all()
            if taken:
                income_id, other = taken[0]
                raise ValueError(
                    f"transaction {income_id} already settles charge {other}. "
                    "One payback cannot discount two charges."
                )

            # A leg of a transfer is money the household moved to itself; it was
            # never anyone's repayment, and counting it as one would discount a
            # real charge with the household's own money.
            link = schema.transfer_link.c
            entangled = conn.execute(
                select(txn.id).where(
                    txn.id.in_([expense_txn_id, *wanted])
                    & (
                        txn.id.in_(
                            select(link.out_txn_id).where(link.tenant_id == context.tenant_id)
                        )
                        | txn.id.in_(
                            select(link.in_txn_id).where(
                                (link.tenant_id == context.tenant_id)
                                & link.in_txn_id.isnot(None)
                            )
                        )
                        | txn.id.in_(
                            select(schema.hidden_txn.c.txn_id).where(
                                schema.hidden_txn.c.tenant_id == context.tenant_id
                            )
                        )
                    )
                )
            ).scalars().all()
            if entangled:
                raise ValueError(
                    f"transaction {entangled[0]} is already a transfer or hidden, "
                    "so it is not part of this charge"
                )

            already = conn.execute(
                select(func.coalesce(func.sum(payback.amount_minor), 0)).where(
                    (payback.tenant_id == context.tenant_id)
                    & (payback.expense_txn_id == expense_txn_id)
                )
            ).scalar_one()
            adding = sum(amounts[i] for i in wanted)
            if already + adding > -charge:
                raise ValueError(
                    f"that is more than the charge: {(already + adding) / 100:,.2f} back "
                    f"against {-charge / 100:,.2f} spent. A charge cannot become income."
                )

            conn.execute(schema.payback_link.insert(), [{
                "tenant_id": context.tenant_id,
                "expense_txn_id": expense_txn_id,
                "income_txn_id": income_id,
                "amount_minor": amounts[income_id],
                "note": note,
                "linked_at": datetime.now(timezone.utc),
            } for income_id in wanted])

        return {
            "linked": len(wanted),
            "paid_back_minor": already + adding,
            "effective_amount_minor": charge + already + adding,
        }

    def unlink_paybacks(
        self, context: TenantContext, expense_txn_id: int, income_txn_id: int | None = None,
    ) -> int:
        """Undo one payback, or every payback on a charge."""
        payback = schema.payback_link.c
        where = (
            (payback.tenant_id == context.tenant_id)
            & (payback.expense_txn_id == expense_txn_id)
        )
        if income_txn_id is not None:
            where = where & (payback.income_txn_id == income_txn_id)
        with self._engine.begin() as conn:
            return conn.execute(schema.payback_link.delete().where(where)).rowcount or 0

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

    def list_manual_transfers(self, context: TenantContext) -> list[dict]:
        """Rows a person said were transfers, so they can say otherwise later.

        Only `origin='manual'`. The matcher's own links are regenerable and are
        not decisions anybody has to be able to walk back — but a manual mark
        removes a row from every figure on one click, and until this existed
        there was no screen anywhere that could name it again, let alone undo
        it. An action with no route back is not a feature.
        """
        link, txn, account = schema.transfer_link.c, schema.txn.c, schema.account.c
        stmt = (
            select(
                link.id, link.out_txn_id, link.in_txn_id, link.amount_minor,
                link.evidence, link.linked_at,
                txn.posted_date, txn.counterparty_norm, txn.amount_minor.label("txn_amount_minor"),
                account.institution,
            )
            .select_from(
                schema.transfer_link
                .join(schema.txn, link.out_txn_id == txn.id)
                .join(schema.account, txn.account_id == account.id)
            )
            .where(
                (link.tenant_id == context.tenant_id) & (link.origin == "manual")
            )
            .order_by(txn.posted_date.desc())
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def category_usage(self, context: TenantContext, name: str) -> dict:
        """How much would break if this category went away."""
        category, rule, enrichment = (
            schema.category.c, schema.category_rule.c, schema.txn_enrichment.c,
        )
        with self._engine.connect() as conn:
            category_id = conn.execute(
                select(category.id).where(
                    (category.tenant_id == context.tenant_id) & (category.name == name)
                )
            ).scalar_one_or_none()
            if category_id is None:
                return {"exists": False, "rules": 0, "transactions": 0}
            return {
                "exists": True,
                "rules": conn.execute(
                    select(func.count()).select_from(schema.category_rule)
                    .where(
                        (rule.tenant_id == context.tenant_id)
                        & (rule.category_id == category_id)
                    )
                ).scalar_one(),
                "transactions": conn.execute(
                    select(func.count()).select_from(schema.txn_enrichment)
                    .where(
                        (enrichment.tenant_id == context.tenant_id)
                        & (enrichment.category == name)
                    )
                ).scalar_one(),
            }

    def delete_category(self, context: TenantContext, name: str) -> bool:
        """Remove a category nothing is using.

        Refuses while anything references it rather than cascading. Deleting a
        category that rows are filed under would either orphan them or silently
        re-file them, and neither is something a person can undo — which is the
        whole reason this exists: adding a category was irreversible without it.
        """
        usage = self.category_usage(context, name)
        if not usage["exists"]:
            return False
        if usage["rules"] or usage["transactions"]:
            raise ValueError(
                f"{name!r} is in use: {usage['rules']} rule(s) and "
                f"{usage['transactions']} transaction(s). Re-file those first — "
                "deleting it would leave them pointing at nothing."
            )
        category = schema.category.c
        with self._engine.begin() as conn:
            removed = conn.execute(
                schema.category.delete().where(
                    (category.tenant_id == context.tenant_id) & (category.name == name)
                )
            ).rowcount
        return bool(removed)

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

            # Everything pointing at these rows goes first, or the delete below
            # fails on a foreign key.
            #
            # It used to clear only `transfer_link`, and SQLite does not enforce
            # foreign keys unless asked to, so nothing noticed. Postgres always
            # does: `reparse` on any document whose rows had been categorised
            # died with a constraint violation, and by then the prod ledger held
            # nearly two thousand enrichment rows.
            #
            # **This loses operator decisions, and that is a known defect rather
            # than a design.** A reparse assigns new `txn.id` values, so a hidden
            # row, a hand-set category or a manual transfer mark cannot follow
            # its transaction across. Re-matching them by `dedupe_key` — which is
            # stable across a reparse and is exactly what it exists for — is the
            # fix, and it is its own piece of work. Recorded in
            # docs/api-contracts.md so it is a decision and not an oversight.
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
                schema.payback_link.delete().where(
                    (schema.payback_link.c.tenant_id == context.tenant_id)
                    & (
                        schema.payback_link.c.expense_txn_id.in_(doomed)
                        | schema.payback_link.c.income_txn_id.in_(doomed)
                    )
                )
            )
            for table in (schema.hidden_txn, schema.txn_enrichment, schema.txn_series_link):
                conn.execute(table.delete().where(table.c.txn_id.in_(doomed)))
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

            # After the accounts exist and before the transactions, so a card
            # number is on record the moment its statement is. `period_end`
            # dates it: the number is what the card carried at the close of
            # this statement, whatever it carries now.
            self._record_card_numbers(
                conn, tenant, account_ids,
                [record.account_key for record in balances]
                + [record.account_key for record in txns],
                document.period_end or document.statement_date or fetched_at.date(),
            )

            inserted, skipped = self._insert_txns(conn, tenant, document_id, account_ids, txns)

        return InsertResult(
            document_id=document_id,
            accounts=len(account_ids),
            txns_inserted=inserted,
            txns_skipped=skipped,
        )

    def _upsert_account(self, conn, tenant_id: int, record: AccountRecord) -> int:
        where = _account_where(tenant_id, record)
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

    def _record_card_numbers(self, conn, tenant_id: int, account_ids: dict, records, seen_on) -> None:
        """Remember every card number these accounts were named by.

        Accumulating, never replacing. A card is reissued and the number
        changes while the account continues, and a payment made to the old
        number still settled this card — so the set only ever grows, and
        `first_seen`/`last_seen` record when each was current.

        Read back by the transfer matcher, which is the only reason any of this
        is kept: a deposit statement records paying the bill against the number,
        so without it nothing joins the payment to the card it paid.

        Widened rather than upserted, because the two engines spell an upsert
        differently and the volume here is a handful of rows per statement —
        Rule 1's seam is worth more than the round trip.
        """
        wanted: dict[tuple[int, str], None] = {}
        for record in records:
            for number in record.card_numbers:
                wanted.setdefault((account_ids[_account_key(record)], number), None)
        if not wanted:
            return

        card = schema.account_card_number.c
        known = {
            (row.account_id, row.card_number_masked): row
            for row in conn.execute(
                select(card.account_id, card.card_number_masked, card.first_seen, card.last_seen)
                .where(
                    (card.tenant_id == tenant_id)
                    & card.account_id.in_({account_id for account_id, _ in wanted})
                )
            )
        }

        fresh = []
        for account_id, number in wanted:
            row = known.get((account_id, number))
            if row is None:
                fresh.append({
                    "tenant_id": tenant_id, "account_id": account_id,
                    "card_number_masked": number,
                    "first_seen": seen_on, "last_seen": seen_on,
                })
            elif seen_on < row.first_seen or seen_on > row.last_seen:
                # Statements arrive in whatever order the operator uploads
                # them, so the span widens from both ends.
                conn.execute(
                    schema.account_card_number.update()
                    .where(
                        (card.tenant_id == tenant_id)
                        & (card.account_id == account_id)
                        & (card.card_number_masked == number)
                    )
                    .values(
                        first_seen=min(seen_on, row.first_seen),
                        last_seen=max(seen_on, row.last_seen),
                    )
                )
        if fresh:
            conn.execute(schema.account_card_number.insert(), fresh)

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


def _account_where(tenant_id: int, record: AccountRecord):
    """Locate an account by the identity it is upserted on."""
    return (
        (schema.account.c.tenant_id == tenant_id)
        & (schema.account.c.institution == record.institution)
        & (schema.account.c.account_ref_masked == record.account_ref_masked)
        & (schema.account.c.sub_account_label == (record.sub_account_label or ""))
        & (schema.account.c.currency == record.currency)
    )


def _account_key(record: AccountRecord) -> tuple:
    return (
        record.institution,
        record.account_ref_masked,
        record.sub_account_label or "",
        record.currency,
    )
