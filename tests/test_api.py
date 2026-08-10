"""The HTTP surface, against the contract in docs/api-contracts.md.

These assert the promises a client is written against, not the shape of the
current implementation. A test here failing means somebody's install breaks.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

pytest.importorskip("fastapi", reason="API extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import (  # noqa: E402
    API_VERSION,
    PREFIX,
    app,
    _config,
    reset_repositories,
)


@pytest.fixture
def client(config, repository):
    """Point the API at whichever database `repository` was parametrised onto.

    The API opens its own handle from the config rather than reusing the
    fixture's, so the config has to name the same database. It did not: it
    always named SQLite, so every one of these ran against an empty file on the
    Postgres pass and asserted the shape of nothing. Twenty-one of them passed
    that way for as long as the dual-engine run went unexercised.
    """
    url = repository.engine.url.render_as_string(hide_password=False)
    against = replace(config, database_url=url)
    app.dependency_overrides[_config] = lambda: against
    # The app pools one engine per URL for the life of the process. Each case
    # here builds its own database, so the pool is dropped between them —
    # otherwise engines accumulate and hold handles on deleted SQLite files.
    reset_repositories()
    with TestClient(app) as c:
        yield c
    reset_repositories()
    app.dependency_overrides.clear()


class TestContract:
    def test_health_states_the_version_it_speaks(self, client):
        """A client uses this to decide whether it can talk to this server,
        which is what stops a client update breaking a self-hoster."""
        body = client.get(f"{PREFIX}/health").json()
        assert body["status"] == "ok"
        assert body["api_version"] == API_VERSION

    def test_the_version_is_in_the_path(self):
        assert PREFIX == f"/api/{API_VERSION}"

    def test_the_root_says_what_this_is(self, client):
        """Visiting the root is the first thing anyone does with a new
        self-hosted service, and answering nothing is how a working install
        looks broken."""
        body = client.get("/").json()
        assert body["api_root"] == PREFIX
        assert body["docs"] == "/docs"

    def test_an_unknown_profile_is_rejected_not_guessed(self, client):
        assert client.get(f"{PREFIX}/accounts", params={"profile": "staging"}).status_code == 400

    def test_growth_reports_a_position_and_says_how_complete_it_is(self, client):
        """Net worth comes from declared balances, never from summing
        transactions — that would read a transfer between one's own accounts as
        growth on one side and loss on the other."""
        body = client.get(f"{PREFIX}/growth", params={"profile": "dummy"}).json()
        assert {"points", "change", "window_months", "currency"} <= set(body)
        for point in body["points"]:
            assert isinstance(point["total_minor"], int)
            # A point is only as current as its stalest account, and a client
            # has to be able to say so.
            assert point["accounts_known"] >= 1


class TestMoney:
    def test_every_amount_is_integer_minor_units(self, client):
        """A JSON float here would reintroduce the error the whole ledger is
        built to avoid."""
        body = client.get(f"{PREFIX}/summary", params={"profile": "dummy"}).json()
        assert isinstance(body["total_minor"], int)
        for row in body["by_category"]:
            assert isinstance(row["total_minor"], int)

    def test_a_summed_total_is_an_integer_on_every_engine(self, client, repository):
        """The case above passes on an empty ledger whatever the engine does,
        because `sum([])` is 0 either way. With rows present, Postgres widens
        `SUM(bigint)` to `numeric` and the total arrives as a JSON **string** —
        which is what a household's real spending did the first time it moved
        off SQLite. Nothing caught it, because no fixture had any money in it.
        """
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="e" * 64, institution="Test", doc_type="acc",
                period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="a.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [TxnRecord(
                account_key=account, posted_date=date(2026, 6, 3), amount_minor=-12345,
                currency="SGD", description_raw="x", description_norm="X",
                counterparty_norm="X", dedupe_key="k", seq=0,
            )],
        )

        body = client.get(f"{PREFIX}/summary", params={"profile": "dummy"}).json()
        assert body["total_minor"] == -12345
        assert isinstance(body["total_minor"], int), (
            f"total_minor came back as {type(body['total_minor']).__name__}"
        )
        for row in body["by_category"]:
            assert isinstance(row["total_minor"], int)

    def test_an_open_range_has_no_average(self, client):
        """An average over an unbounded period is not a small number, it is
        not a number."""
        body = client.get(f"{PREFIX}/summary", params={"profile": "dummy"}).json()
        assert body["average_minor"]["per_day"] is None

    def test_averages_span_the_range_the_totals_do(self, client):
        body = client.get(
            f"{PREFIX}/summary",
            params={"profile": "dummy", "since": "2026-01-01", "until": "2026-01-31"},
        ).json()
        assert body["range"]["days"] == 31
        assert body["average_minor"]["per_day"] is not None


class TestFilters:
    @pytest.mark.parametrize("params", [
        {"since": "2026-01-01"},
        {"until": "2026-12-31"},
        {"account_id": 1},
        {"category": "Grocery"},
    ])
    def test_every_figure_is_derivable_from_the_three_axes(self, client, params):
        """§5.1(B): date range, accounts, categories — on demand, not
        precomputed."""
        response = client.get(f"{PREFIX}/summary", params={"profile": "dummy", **params})
        assert response.status_code == 200

    def test_transactions_page(self, client):
        body = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy", "limit": 5}
        ).json()
        assert body["limit"] == 5 and body["offset"] == 0
        assert isinstance(body["transactions"], list)

    @pytest.mark.parametrize("direction", ["out", "in", "net"])
    def test_every_direction_is_accepted_on_both(self, client, direction):
        for path in ("/summary", "/transactions"):
            response = client.get(
                f"{PREFIX}{path}", params={"profile": "dummy", "direction": direction}
            )
            assert response.status_code == 200, path

    def test_an_unknown_direction_is_refused_not_defaulted(self, client):
        """Silently falling back to spending would show a client asking for
        income a screen full of negatives that looked like an answer."""
        response = client.get(
            f"{PREFIX}/summary", params={"profile": "dummy", "direction": "profit"}
        )
        assert response.status_code == 422

    def test_omitting_direction_means_spending(self, client):
        """The parameter was added after clients existed. Older ones send
        nothing and must keep getting the screen they were written for."""
        plain = client.get(f"{PREFIX}/summary", params={"profile": "dummy"}).json()
        explicit = client.get(
            f"{PREFIX}/summary", params={"profile": "dummy", "direction": "out"}
        ).json()
        assert plain["total_minor"] == explicit["total_minor"]


class TestSearch:
    """Finding one line item, which the review queue cannot do.

    `/review` groups by counterparty and so has no date — by design, because it
    asks what a merchant is. The other question, "what was that charge and when",
    needs the ledger itself.
    """

    @pytest.fixture
    def ledger(self, repository):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        rows = [
            # Long ago, so a six-month window would hide it.
            (date(2022, 3, 4), -24800, "IKEA TAMPINES", "IKEA TAMPINES SI NG 04MAR"),
            (date(2026, 7, 2), -1860, "IKEA-RESTAURANT", "CARD TRANSACTION IKEA-RESTAURANT"),
            # Normalisation strips the reference, so this is findable only by
            # what the statement actually printed.
            (date(2026, 7, 3), -5000, "SOME SHOP", "GIRO PAYMENT REF Z8891 SOME SHOP"),
            (date(2026, 7, 4), -700, "100% OFF SALE", "100% OFF SALE"),
        ]
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="d" * 64, institution="Test", doc_type="acc",
                period_start=date(2022, 1, 1), period_end=date(2026, 12, 31),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="a.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=day, amount_minor=amount,
                    currency="SGD", description_raw=raw, description_norm=norm,
                    counterparty_norm=norm, dedupe_key=f"s{i}", seq=i,
                )
                for i, (day, amount, norm, raw) in enumerate(rows)
            ],
        )
        return context

    def _search(self, client, q, **extra):
        body = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy", "q": q, **extra}
        ).json()
        return [t["counterparty_norm"] for t in body["transactions"]]

    def test_it_finds_a_charge_outside_the_window(self, client, ledger):
        """The reason a search ignores the date range: somebody searching for a
        merchant is searching precisely because they do not know which month it
        was in. Confining that to the last six would return nothing and look
        like an answer."""
        found = self._search(client, "ikea", since="2026-01-01", until="2026-12-31")
        assert "IKEA TAMPINES" in found

    def test_a_search_says_its_range_is_open(self, client, ledger):
        """So a client can explain the null averages rather than look broken."""
        body = client.get(
            f"{PREFIX}/summary",
            params={"profile": "dummy", "q": "ikea", "since": "2026-01-01"},
        ).json()
        assert body["range"]["since"] is None
        assert body["q"] == "ikea"

    def test_it_is_case_insensitive(self, client, ledger):
        assert self._search(client, "IkEa") == self._search(client, "ikea")

    def test_it_searches_what_the_statement_printed(self, client, ledger):
        """Normalisation strips references and mechanism words, so the text the
        operator remembers seeing often survives only in description_raw."""
        assert self._search(client, "Z8891") == ["SOME SHOP"]

    def test_a_percent_sign_is_a_percent_sign(self, client, ledger):
        """Unescaped, `%` is a LIKE wildcard and this would quietly match the
        entire ledger while looking like a working search."""
        assert self._search(client, "100%") == ["100% OFF SALE"]

    def test_an_underscore_is_not_a_wildcard(self, client, ledger):
        assert self._search(client, "IKEA_") == []

    def test_every_figure_on_screen_honours_it(self, client, ledger):
        """The totals, the chart and the list are one screen. A search that
        narrowed only the list would leave them describing different sets of
        transactions — the failure §5.1(A) exists to prevent."""
        params = {"profile": "dummy", "q": "ikea"}
        summary = client.get(f"{PREFIX}/summary", params=params).json()
        trend = client.get(f"{PREFIX}/trend", params=params).json()
        listing = client.get(f"{PREFIX}/transactions", params=params).json()

        assert summary["total_minor"] == -24800 - 1860
        assert sum(p["out_minor"] for p in trend["points"]) == summary["total_minor"]
        assert len(listing["transactions"]) == 2

    def test_the_raw_description_comes_back(self, client, ledger):
        """A normalised name is often not what the operator remembers, so the
        row has to be able to show both."""
        body = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy", "q": "Z8891"}
        ).json()
        assert body["transactions"][0]["description_raw"].startswith("GIRO PAYMENT")

    def test_blank_search_is_no_search(self, client, ledger):
        """Whitespace in the box must not silently discard the date range."""
        body = client.get(
            f"{PREFIX}/summary",
            params={"profile": "dummy", "q": "   ", "since": "2026-01-01", "until": "2026-12-31"},
        ).json()
        assert body["q"] is None
        assert body["range"]["since"] == "2026-01-01"


class TestDeciding:
    def test_an_unknown_category_is_refused_with_the_known_ones(self, client):
        """A client should be able to show the user what it may choose."""
        response = client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "SHOP", "category": "Nonsense"},
        )
        assert response.status_code == 422
        assert "Grocery" in response.json()["detail"]["known"]

    def test_deciding_is_recorded_and_applied(self, client):
        """`applied` is the number of rows the rules now account for.

        It used to be `False` always: applying was a separate pass, which saved
        a write per click and left the spending list disagreeing with the
        review queue until somebody remembered to run it.
        """
        body = client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "SOME SHOP", "category": "Grocery"},
        ).json()
        assert body["created"] is True and isinstance(body["applied"], int)

    def test_deciding_twice_is_not_an_error(self, client):
        params = {"profile": "dummy", "counterparty": "TWICE", "category": "Dining"}
        client.post(f"{PREFIX}/review/decide", params=params)
        again = client.post(f"{PREFIX}/review/decide", params=params).json()
        assert again["created"] is False


class TestADecisionReachesTheLedger:
    """Deciding in Review has to change the expenses list.

    It did not. The decision was stored as a rule and the rule was never
    applied, so the review queue — which reads rules — showed the merchant
    settled while the spending list — which reads `txn_enrichment` — still
    showed it uncategorised. The operator decided a merchant, watched it leave
    the queue, then met the same merchant uncategorised on the other page and
    did it again by hand, over and over.
    """

    def _seed(self, repository, counterparty):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="a" * 64, institution="Test", doc_type="acc",
                period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="a.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=date(2026, 6, day),
                    amount_minor=-1000 * day, currency="SGD",
                    description_raw=counterparty, description_norm=counterparty,
                    counterparty_norm=counterparty, dedupe_key=f"seed{day}", seq=day,
                )
                for day in (3, 10, 17)
            ],
        )

    def _categories(self, client):
        return {
            row["id"]: row.get("category")
            for row in client.get(
                f"{PREFIX}/transactions", params={"profile": "dummy", "limit": 100},
            ).json()["transactions"]
        }

    def test_deciding_categorises_every_matching_row(self, client, repository):
        self._seed(repository, "REPEATED SHOP")
        assert set(self._categories(client).values()) == {None}

        body = client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "REPEATED SHOP",
                    "category": "Grocery"},
        ).json()

        assert body["applied"] >= 3, "the decision has to reach the ledger, not just the rules"
        assert set(self._categories(client).values()) == {"Grocery"}

    def test_the_expenses_list_can_be_filtered_by_it_at_once(self, client, repository):
        """The category filter reads the same column, so a decision that never
        landed made the filter return nothing and look broken."""
        self._seed(repository, "FILTERABLE SHOP")
        client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "FILTERABLE SHOP",
                    "category": "Grocery"},
        )
        rows = client.get(
            f"{PREFIX}/transactions",
            params={"profile": "dummy", "category": "Grocery"},
        ).json()["transactions"]
        assert len(rows) == 3

    def test_taking_the_decision_back_uncategorises_them_again(self, client, repository):
        """Or the rows keep wearing a category no rule stands behind."""
        self._seed(repository, "REGRETTED SHOP")
        client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "REGRETTED SHOP",
                    "category": "Grocery"},
        )
        assert set(self._categories(client).values()) == {"Grocery"}

        client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "REGRETTED SHOP"},
        )
        assert set(self._categories(client).values()) == {None}

    def test_the_summary_moves_with_it(self, client, repository):
        """Every figure on screen reads the same column, so none of them may
        lag a decision."""
        self._seed(repository, "TOTALLED SHOP")
        client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "TOTALLED SHOP",
                    "category": "Grocery"},
        )
        body = client.get(f"{PREFIX}/summary", params={"profile": "dummy"}).json()
        grocery = [r for r in body["by_category"] if r["category"] == "Grocery"]
        assert grocery and grocery[0]["total_minor"] == -(3000 + 10000 + 17000)


class TestUndeciding:
    """Deciding was a one-way door: the rule went in and no route could name it
    again, let alone remove it. Contract rule 2a — an inverse *and* a listing."""

    def _decide(self, client, counterparty, category="Grocery"):
        return client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": counterparty, "category": category},
        )

    def test_a_decision_appears_in_the_listing(self, client):
        self._decide(client, "FINDABLE SHOP")
        body = client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()
        mine = [r for r in body["rules"] if r["counterparty"] == "FINDABLE SHOP"]
        assert len(mine) == 1
        assert mine[0]["category"] == "Grocery"
        assert mine[0]["origin"] == "operator"

    def test_the_listing_shows_the_name_not_the_pattern(self, client):
        """A person is shown their own decision, not its implementation."""
        self._decide(client, "A-SHOP (X)")
        rule = next(
            r for r in client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()["rules"]
            if r["counterparty"] == "A-SHOP (X)"
        )
        assert "\\" in rule["pattern"]  # escaped, as a decision must be
        assert rule["counterparty"] == "A-SHOP (X)"

    def test_taking_it_back_removes_it(self, client):
        self._decide(client, "REGRETTED")
        undone = client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "REGRETTED"},
        ).json()
        assert undone["removed"] == 1
        assert undone["was"] == [{"pattern": r"^REGRETTED$", "category": "Grocery"}]

        body = client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()
        assert not [r for r in body["rules"] if r["counterparty"] == "REGRETTED"]

    def test_taking_back_nothing_is_not_an_error(self, client):
        """A second click on undo has to do what the first one did."""
        self._decide(client, "ONCE")
        for _ in range(2):
            body = client.request(
                "DELETE", f"{PREFIX}/review/decide",
                params={"profile": "dummy", "counterparty": "ONCE"},
            )
            assert body.status_code == 200
        assert body.json()["removed"] == 0

    def test_the_name_is_matched_however_it_is_cased(self, client):
        self._decide(client, "MIXED Case")
        undone = client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "mixed case"},
        ).json()
        assert undone["removed"] == 1

    def test_deciding_again_after_undoing_works(self, client):
        """Undo, then redo. The uniqueness constraint must not have kept the
        name spoken for after the rule was removed."""
        self._decide(client, "REDECIDED")
        client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "REDECIDED"},
        )
        again = self._decide(client, "REDECIDED", "Dining").json()
        assert again["created"] is True

    def test_undeciding_reaches_the_ledger_too(self, client):
        """Symmetric with deciding, and both now write through.

        They used to write through neither, so a queue could be worked and
        reworked for one pass at the end. That saved a write per click and cost
        the operator the truth: the spending list went on showing what the
        rules no longer said, and the same merchant had to be categorised by
        hand a second time. `applied` is a row count, so `0` is a real answer
        for a ledger with nothing matching.
        """
        self._decide(client, "SYMMETRIC")
        undone = client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "SYMMETRIC"},
        ).json()
        assert isinstance(undone["applied"], int)

    def test_an_imported_rule_is_not_offered_as_a_decision(self, client):
        """It was nobody's decision. Undoing one would promise something the
        next import takes straight back — the same reason the matcher's own
        transfer links are not listed."""
        client.post(
            f"{PREFIX}/rules",
            params={"profile": "dummy", "pattern": "^SEEDED$", "category": "Grocery",
                    "note": "agreed by 3/3 models"},
        )
        listed = client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()
        assert not [r for r in listed["rules"] if r["counterparty"] == "SEEDED"]

        everything = client.get(
            f"{PREFIX}/rules", params={"profile": "dummy", "origin": "all"}
        ).json()
        seeded = next(r for r in everything["rules"] if r["counterparty"] == "SEEDED")
        assert seeded["origin"] == "imported"

    def test_undeciding_leaves_an_imported_rule_alone(self, client):
        client.post(
            f"{PREFIX}/rules",
            params={"profile": "dummy", "pattern": "^BORROWED$", "category": "Grocery"},
        )
        undone = client.request(
            "DELETE", f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "BORROWED"},
        ).json()
        assert undone["removed"] == 0

    def test_a_deleted_rule_says_enough_to_put_it_back(self, client):
        """An id means nothing once the row is gone, so undo has to travel with
        the response or it is a promise the client cannot keep."""
        created = client.post(
            f"{PREFIX}/rules",
            params={"profile": "dummy", "pattern": "^RESTORE ME$", "category": "Dining",
                    "weight": 7, "note": "why"},
        ).json()
        gone = client.delete(
            f"{PREFIX}/rules/{created['id']}", params={"profile": "dummy"}
        ).json()
        assert gone["deleted"] is True
        assert gone["was"] == {
            "pattern": "^RESTORE ME$", "category": "Dining", "weight": 7, "note": "why",
        }

        back = client.post(f"{PREFIX}/rules", params={"profile": "dummy", **gone["was"]}).json()
        assert back["created"] is True
        # A new row. Its id is whatever the engine assigned — SQLite happily
        # reuses the one just freed — so the response names it rather than
        # letting a client carry on with the one it was holding.
        assert back["id"] is not None
        restored = client.get(
            f"{PREFIX}/rules", params={"profile": "dummy", "origin": "all"}
        ).json()["rules"]
        assert [r for r in restored if r["id"] == back["id"]][0]["pattern"] == "^RESTORE ME$"

    def test_deleting_a_rule_that_is_gone_is_not_an_error(self, client):
        body = client.delete(f"{PREFIX}/rules/999999", params={"profile": "dummy"}).json()
        assert body == {"rule_id": 999999, "deleted": False, "was": None, "applied": 0}

    def test_a_pattern_that_is_not_an_expression_is_refused(self, client):
        """Rather than stored to throw on the next categorisation pass."""
        response = client.post(
            f"{PREFIX}/rules", params={"profile": "dummy", "pattern": "^(unclosed", "category": "Dining"},
        )
        assert response.status_code == 422

    def test_an_unknown_category_is_refused_with_the_known_ones(self, client):
        response = client.post(
            f"{PREFIX}/rules",
            params={"profile": "dummy", "pattern": "^X$", "category": "Nonsense"},
        )
        assert response.status_code == 422
        assert "Grocery" in response.json()["detail"]["known"]

    def test_the_listing_says_how_much_a_decision_covers(self, client, repository):
        """"This covers 47 rows" is what makes removing one a considered act."""
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="c" * 64, institution="Test", doc_type="acc",
                period_start=date(2026, 6, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="a.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=date(2026, 6, 3 + i),
                    amount_minor=-1000, currency="SGD",
                    description_raw="COUNTED SHOP", description_norm="COUNTED SHOP",
                    counterparty_norm="COUNTED SHOP", dedupe_key=f"c{i}", seq=i,
                )
                for i in range(3)
            ],
        )
        self._decide(client, "COUNTED SHOP")
        rule = next(
            r for r in client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()["rules"]
            if r["counterparty"] == "COUNTED SHOP"
        )
        assert rule["transactions"] == 3

    def test_a_rule_with_no_plain_name_reports_no_count(self, client):
        """Null rather than zero. A real expression would need a scan to count
        against, and a zero would read as "covers nothing", which is a
        different claim from "not counted"."""
        client.post(
            f"{PREFIX}/rules",
            params={"profile": "dummy", "pattern": "SHOP|STORE", "category": "Grocery"},
        )
        rule = next(
            r for r in client.get(
                f"{PREFIX}/rules", params={"profile": "dummy", "origin": "all"}
            ).json()["rules"]
            if r["pattern"] == "SHOP|STORE"
        )
        assert rule["counterparty"] is None and rule["transactions"] is None

    def test_the_listing_can_be_searched(self, client):
        self._decide(client, "NEEDLE SHOP")
        self._decide(client, "HAYSTACK SHOP")
        found = client.get(
            f"{PREFIX}/rules", params={"profile": "dummy", "q": "needle"}
        ).json()
        assert [r["counterparty"] for r in found["rules"]] == ["NEEDLE SHOP"]

    def test_the_newest_decision_is_first(self, client):
        """The rule somebody wants to find is nearly always the one they just
        wrote, and weight order buries it among everything else at 100."""
        self._decide(client, "OLDER")
        self._decide(client, "NEWER")
        listed = client.get(f"{PREFIX}/rules", params={"profile": "dummy"}).json()
        names = [r["counterparty"] for r in listed["rules"]]
        assert names.index("NEWER") < names.index("OLDER")


class TestUploading:
    """Statements over HTTP, because the alternative was `scp` and a shell —
    a fine answer for the person who built this and a poor one for the person
    living with it."""

    def _post(self, client, *files):
        return client.post(
            f"{PREFIX}/documents",
            params={"profile": "dummy"},
            files=[("files", (name, body, kind)) for name, body, kind in files],
        )

    def test_a_file_that_is_not_a_statement_is_refused_before_anything_is_written(self, client):
        """An extension is a claim; the magic bytes are what it is. The parser
        is the wrong place to find out."""
        response = self._post(client, ("notes.pdf", b"just some text", "application/pdf"))
        assert response.status_code == 201
        body = response.json()
        assert body["accepted"] == 0
        assert body["rejected"][0]["filename"] == "notes.pdf"
        assert "not a statement" in body["rejected"][0]["reason"]

    def test_an_empty_file_is_refused(self, client):
        body = self._post(client, ("empty.pdf", b"", "application/pdf")).json()
        assert body["rejected"][0]["reason"] == "empty"

    def test_a_pdf_is_accepted_even_when_nothing_can_parse_it(self, client):
        """Quarantine is a normal outcome, not a failure of this endpoint. A
        layout with no adapter yet is set aside with a reason and the run
        continues, which is the whole ingestion design."""
        body = self._post(client, ("statement.pdf", b"%PDF-1.4 nonsense", "application/pdf")).json()
        assert body["accepted"] == 1
        assert body["rejected"] == []
        assert len(body["documents"]) == 1

    def test_every_file_is_reported_on_its_own(self, client):
        """A batch of three with one bad file in it must not read as three
        failures."""
        body = self._post(
            client,
            ("a.pdf", b"%PDF-1.4 aaa", "application/pdf"),
            ("b.txt", b"not a statement", "text/plain"),
            ("c.pdf", b"%PDF-1.4 ccc", "application/pdf"),
        ).json()
        assert body["accepted"] == 2
        assert [r["filename"] for r in body["rejected"]] == ["b.txt"]

    def test_a_path_in_the_filename_cannot_escape_the_inbox(self, client):
        """The operator did not choose the name their bank generated, so the
        separators are stripped rather than the upload refused."""
        body = self._post(
            client, ("../../etc/passwd.pdf", b"%PDF-1.4 x", "application/pdf")
        ).json()
        assert body["accepted"] == 1
        assert "/" not in body["documents"][0]["filename"]
        assert ".." not in body["documents"][0]["filename"]

    def test_uploading_the_same_bytes_twice_is_not_an_error(self, client):
        """Re-uploading what is already imported is what somebody does when
        they are not sure whether they did."""
        pdf = ("same.pdf", b"%PDF-1.4 identical", "application/pdf")
        first = self._post(client, pdf).json()
        second = self._post(client, pdf).json()
        assert first["accepted"] == second["accepted"] == 1
        assert second["rejected"] == []

    def test_what_is_in_the_ledger_can_be_listed(self, client):
        body = client.get(f"{PREFIX}/documents", params={"profile": "dummy"}).json()
        assert {"total", "documents"} <= set(body)

    def test_removing_a_document_that_is_not_there_is_not_an_error(self, client):
        """The same shape as unhiding twice."""
        body = client.delete(
            f"{PREFIX}/documents/{'f' * 64}", params={"profile": "dummy"}
        ).json()
        assert body == {"sha256": "f" * 64, "deleted": False, "transfers": None}


class TestRematchingTransfers:
    def test_it_reports_and_does_not_write_by_default(self, client):
        """The pass changes what the ledger *means* — a linked pair stops
        counting as spending — so a client can show that before it is true."""
        body = client.post(
            f"{PREFIX}/transfers/rematch", params={"profile": "dummy"}
        ).json()
        assert body["applied"] is False
        assert {"added", "removed", "unchanged", "found", "manual"} <= set(body)

    def test_the_response_carries_the_diff_not_only_the_total(self, client):
        """"206 links" says nothing about whether to apply it."""
        body = client.post(
            f"{PREFIX}/transfers/rematch", params={"profile": "dummy"}
        ).json()
        assert isinstance(body["added"], int)
        assert isinstance(body["removed"], int)

    def test_each_kind_of_evidence_has_its_own_window(self, client):
        body = client.post(
            f"{PREFIX}/transfers/rematch",
            params={"profile": "dummy", "max_days": 2, "card_days": 45},
        ).json()
        assert body["window"] == {
            "min_days": 0, "max_days": 2, "named_days": 30, "card_days": 45,
        }

    def test_an_impossible_window_is_refused_with_the_reason(self, client):
        """Silently pairing nothing would look like a working matcher with an
        empty ledger."""
        response = client.post(
            f"{PREFIX}/transfers/rematch",
            params={"profile": "dummy", "min_days": 200},
        )
        assert response.status_code == 422
        assert "nothing could ever pair" in response.json()["detail"]["error"]

    def test_a_window_out_of_range_is_refused(self, client):
        response = client.post(
            f"{PREFIX}/transfers/rematch", params={"profile": "dummy", "max_days": 4000},
        )
        assert response.status_code == 422

    def test_the_window_in_force_is_readable(self, client):
        """A rule nobody can read is a rule nobody can fix."""
        body = client.get(f"{PREFIX}/transfers", params={"profile": "dummy"}).json()
        assert body["window"] == body["defaults"]
        assert {"linked", "manual"} <= set(body)

    def test_a_saved_window_is_what_the_next_run_uses(self, client):
        client.post(
            f"{PREFIX}/transfers/rematch",
            params={"profile": "dummy", "card_days": 45, "apply": True, "save": True},
        )
        after = client.get(f"{PREFIX}/transfers", params={"profile": "dummy"}).json()
        assert after["window"]["card_days"] == 45
        assert after["defaults"]["card_days"] == 21  # unchanged, and still shown

    def test_saving_needs_applying(self, client):
        """A window remembered from a preview would mean the ledger and the
        stored rule disagreed about what had been decided."""
        body = client.post(
            f"{PREFIX}/transfers/rematch",
            params={"profile": "dummy", "card_days": 45, "save": True},
        ).json()
        assert body["saved"] is False
        after = client.get(f"{PREFIX}/transfers", params={"profile": "dummy"}).json()
        assert after["window"]["card_days"] == 21

    def test_a_window_equal_to_the_default_is_not_stored(self, client):
        """Storing it would pin this install to today's default forever, and a
        later improvement to that default would reach nobody."""
        client.post(
            f"{PREFIX}/transfers/rematch",
            params={"profile": "dummy", "card_days": 21, "apply": True, "save": True},
        )
        body = client.get(f"{PREFIX}/transfers", params={"profile": "dummy"}).json()
        assert body["window"] == body["defaults"]


class TestServingTheClient:
    """One image carries both, which is what makes one container enough.

    The failure this guards against is subtle: a catch-all that serves the page
    for *every* unmatched path would answer a mistyped API call with HTML and a
    200 on it, and a client would parse the page it is running in as a ledger.
    """

    @pytest.fixture
    def built(self, tmp_path, monkeypatch):
        from app.api import main

        (tmp_path / "assets").mkdir()
        (tmp_path / "index.html").write_text("<!doctype html><title>Finstone</title>")
        monkeypatch.setattr(main, "WEB_ROOT", tmp_path)
        return tmp_path

    def test_the_root_is_the_app_when_one_is_carried(self, client, built):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_the_page_is_never_cached(self, client, built):
        """The assets beside it are content-hashed and cached for a year; this
        file names them. Cache it and an upgraded container keeps serving the
        previous build to a returning browser."""
        assert client.get("/").headers["cache-control"] == "no-store"

    def test_a_deep_link_returns_the_app(self, client, built):
        """A single-page app owns its own routing, so a refresh on any screen
        but the first has to return the page rather than a 404."""
        response = client.get("/some/screen/deep/in/the/app")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_an_unknown_api_path_is_still_a_json_404(self, client, built):
        """Not the page. A client that got HTML here would try to read it."""
        response = client.get(f"{PREFIX}/nonsense")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")

    def test_the_schema_is_still_reachable(self, client, built):
        assert client.get("/openapi.json").status_code == 200

    def test_the_root_is_the_api_when_no_client_is_carried(self, client):
        """A `pip install` and every test get this. The JSON is the honest
        answer when there is no page to serve, and a bare 404 would make a
        working install look broken."""
        body = client.get("/").json()
        assert body["service"] == "finstone"
        assert body["api_root"] == PREFIX

    def test_an_unknown_path_says_there_is_no_client(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 404
        assert "carries no client" in response.json()["detail"]


class TestTrend:
    def test_the_response_says_what_window_the_line_used(self, client):
        """A client labels the line from this. Deriving it client-side would
        put the rule in two places and let them disagree."""
        body = client.get(
            f"{PREFIX}/trend", params={"profile": "dummy", "bucket": "month"}
        ).json()
        assert body["rolling_window"] >= 1
        assert body["bucket"] == "month"

    def test_a_point_carries_all_three_directions(self, client):
        """`direction` is deliberately not a trend parameter: switching the
        chart between spending, income and net must not re-fetch and risk two
        series bucketed differently."""
        body = client.get(
            f"{PREFIX}/trend", params={"profile": "dummy", "bucket": "month"}
        ).json()
        for point in body["points"]:
            assert {"out_minor", "in_minor", "net_minor"} <= set(point)
            assert point["net_minor"] == point["out_minor"] + point["in_minor"]
            assert set(point["rolling"]) == {"out_minor", "in_minor", "net_minor"}
            assert 1 <= point["rolling_of"] <= body["rolling_window"]


class TestShape:
    def test_review_says_what_deciding_is_worth(self, client):
        body = client.get(f"{PREFIX}/review", params={"profile": "dummy"}).json()
        assert {"outstanding", "value_at_stake_minor", "items"} <= set(body)

    def test_recurring_separates_lapsed_from_overdue(self, client):
        """A cancelled subscription and a skipped payment want opposite
        reactions."""
        body = client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()
        assert {"series", "due_soon", "overdue", "lapsed"} <= set(body)
        assert isinstance(body["monthly_commitment_minor"], int)


class TestSayingItIsNotASubscription:
    """Detection is a good guess and still a guess.

    Widening it to find direct debits the bank never named was a deliberate
    trade: more real commitments, and the occasional wrong one. The answer to
    the wrong ones is not a stricter detector — that goes back to missing seven
    insurance premiums — but a cheap way to say no.
    """

    def _seed_a_series(self, repository, counterparty="YEARLY THING"):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="d" * 64, institution="Test", doc_type="acc",
                period_start=date(2023, 1, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="d.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=date(year, 6, 15),
                    amount_minor=-2300, currency="SGD",
                    description_raw=counterparty, description_norm=counterparty,
                    counterparty_norm=counterparty, dedupe_key=f"yr{year}", seq=year,
                )
                for year in (2023, 2024, 2025)
            ],
        )

    def _recurring(self, client):
        return client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()

    def _names(self, body):
        return {
            s["merchant"]
            for s in body["series"] + body["lapsed"] + body["overdue"] + body["due_soon"]
        }

    def test_a_dismissed_series_leaves_every_list_and_the_total(self, client, repository):
        self._seed_a_series(repository)
        before = self._recurring(client)
        assert "YEARLY THING" in self._names(before)

        client.post(
            f"{PREFIX}/recurring/dismiss",
            params={"profile": "dummy", "merchant": "YEARLY THING",
                    "amount_centre_minor": 2300},
        )
        after = self._recurring(client)
        assert "YEARLY THING" not in self._names(after)
        assert after["monthly_commitment_minor"] != before["monthly_commitment_minor"]             or before["monthly_commitment_minor"] == 0

    def test_it_can_be_found_again(self, client, repository):
        """Contract rule 2a: an undo nobody can reach is not an undo. Without a
        listing the series would vanish with nothing to name it by."""
        self._seed_a_series(repository)
        client.post(
            f"{PREFIX}/recurring/dismiss",
            params={"profile": "dummy", "merchant": "YEARLY THING",
                    "amount_centre_minor": 2300},
        )
        body = client.get(f"{PREFIX}/recurring/dismissed", params={"profile": "dummy"}).json()
        assert body["total"] == 1
        assert body["dismissed"][0]["merchant_norm"] == "YEARLY THING"

    def test_putting_it_back_restores_it_exactly(self, client, repository):
        """The pass stays a pure function of the ledger, so the series is still
        being computed — a dismissal only removes it from the reading."""
        self._seed_a_series(repository)
        before = self._recurring(client)
        params = {"profile": "dummy", "merchant": "YEARLY THING",
                  "amount_centre_minor": 2300}

        client.post(f"{PREFIX}/recurring/dismiss", params=params)
        restored = client.request(
            "DELETE", f"{PREFIX}/recurring/dismiss", params=params,
        ).json()

        assert restored["restored"] is True
        assert self._recurring(client)["series"] == before["series"]

    def test_dismissing_twice_is_not_an_error(self, client, repository):
        self._seed_a_series(repository)
        params = {"profile": "dummy", "merchant": "YEARLY THING",
                  "amount_centre_minor": 2300}
        assert client.post(f"{PREFIX}/recurring/dismiss", params=params).json()["dismissed"] is True
        assert client.post(f"{PREFIX}/recurring/dismiss", params=params).json()["dismissed"] is False

    def test_restoring_something_that_was_never_dismissed_is_not_an_error(self, client):
        body = client.request(
            "DELETE", f"{PREFIX}/recurring/dismiss",
            params={"profile": "dummy", "merchant": "NOTHING", "amount_centre_minor": 1},
        ).json()
        assert body["restored"] is False

    def test_a_dismissal_does_not_touch_the_transactions(self, client, repository):
        """It is a fact about the *reading*, not about the rows."""
        self._seed_a_series(repository)
        before = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy"},
        ).json()["transactions"]
        client.post(
            f"{PREFIX}/recurring/dismiss",
            params={"profile": "dummy", "merchant": "YEARLY THING",
                    "amount_centre_minor": 2300},
        )
        after = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy"},
        ).json()["transactions"]
        assert [r["id"] for r in after] == [r["id"] for r in before]


class TestSayingItIsASubscription:
    """The other half, and needed for the same reason from the other side.

    The detector wants three occurrences and gaps that barely vary. That is the
    right bar for a guess — and it means a yearly premium is invisible for two
    years, and a plan started last month cannot be seen at all. Loosening the
    detector to reach those is what turned one monthly premium into three
    quarterly ones; the operator knew the answer the whole time.
    """

    MERCHANT = "SOME INSURER"

    def _seed(self, repository, dates, amount=-27386):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="e" * 64, institution="Test", doc_type="acc",
                period_start=date(2023, 1, 1), period_end=date(2026, 12, 31),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="e.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=day, amount_minor=amount,
                    currency="SGD", description_raw=self.MERCHANT,
                    description_norm=self.MERCHANT, counterparty_norm=self.MERCHANT,
                    dedupe_key=f"m{index}", seq=index,
                )
                for index, day in enumerate(dates)
            ],
        )
        return context

    def _recurring(self, client):
        return client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()

    def _series(self, client):
        """Across every list. A marked series is subject to the same lapsing as
        a detected one — a monthly commitment last paid a year ago reads as
        cancelled whoever said it was monthly — so which list it lands in is a
        fact about the dates, not about the mark."""
        body = self._recurring(client)
        return {s["merchant"]: s for s in body["series"] + body["lapsed"]}

    def _mark(self, client, period="yearly", **extra):
        return client.post(f"{PREFIX}/recurring/mark", params={
            "profile": "dummy", "merchant": self.MERCHANT,
            "amount_centre_minor": 27386, "period": period, **extra,
        })

    def test_two_occurrences_are_a_subscription_when_a_person_says_so(
        self, client, repository,
    ):
        """The case the detector cannot reach and the operator can: a yearly
        premium is invisible until its third year."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        assert self.MERCHANT not in self._series(client)

        assert self._mark(client).json()["marked"] is True
        series = self._series(client)
        assert self.MERCHANT in series
        assert series[self.MERCHANT]["period_label"] == "yearly"
        assert series[self.MERCHANT]["occurrences"] == 2

    def test_the_period_is_the_operator_s_and_not_inferred(self, client, repository):
        """Two rows a year apart marked as monthly stay monthly. The gaps are
        not evidence here — that is the point of a mark — and quietly
        correcting the period to the one the dates imply would make the feature
        useless for the irregular billing it exists for."""
        self._seed(repository, [date(2025, 1, 10), date(2026, 1, 10)])
        self._mark(client, period="monthly")
        assert self._series(client)[self.MERCHANT]["period_label"] == "monthly"

    def test_it_counts_toward_the_monthly_commitment(self, client, repository):
        """A commitment nobody totals is a commitment nobody has accounted
        for."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        before = self._recurring(client)["monthly_commitment_minor"]
        self._mark(client)
        after = self._recurring(client)["monthly_commitment_minor"]
        assert after - before == round(27386 / 12)

    def test_a_marked_series_is_not_counted_twice(self, client, repository):
        """Marking something the detector already found must not produce both
        readings: the monthly total is the number this page exists to state."""
        self._seed(repository, [date(2024, 1, 10), date(2024, 2, 10), date(2024, 3, 10)])
        detected = self._recurring(client)
        every = lambda body: [
            s["merchant"] for s in body["series"] + body["lapsed"]
        ]
        assert self.MERCHANT in every(detected)

        self._mark(client, period="monthly")
        after = self._recurring(client)
        assert every(after).count(self.MERCHANT) == 1
        assert after["monthly_commitment_minor"] == detected["monthly_commitment_minor"]

    def test_the_screen_says_who_decided(self, client, repository):
        """A client offers to un-mark what a person marked and to dismiss what
        the detector found. It cannot do that without being told which."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        self._mark(client)
        assert self._series(client)[self.MERCHANT]["marked_by"] == "operator"

    def test_confidence_does_not_claim_evidence_it_lacks(self, client, repository):
        """`confidence` describes how little the gaps varied, and two dates
        have one gap, which never varies. Reporting 1.0 would dress the
        operator's assertion up as the strongest possible evidence."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        self._mark(client)
        assert self._series(client)[self.MERCHANT]["confidence"] == 0.0

    def test_unmarking_puts_everything_back(self, client, repository):
        """Un-marking has to be as cheap as marking: a page somebody is afraid
        to touch is one that stays wrong."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        before = self._recurring(client)
        self._mark(client)
        body = client.request("DELETE", f"{PREFIX}/recurring/mark", params={
            "profile": "dummy", "merchant": self.MERCHANT,
            "amount_centre_minor": 27386,
        }).json()

        assert body["unmarked"] is True
        assert self._recurring(client) == before

    def test_marking_twice_is_not_an_error(self, client, repository):
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        assert self._mark(client).json()["marked"] is True
        assert self._mark(client).json()["marked"] is False

    def test_a_different_period_corrects_rather_than_duplicates(self, client, repository):
        """Somebody choosing quarterly after monthly is fixing a mistake, not
        taking out a second subscription against the same charges."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        self._mark(client, period="monthly")
        self._mark(client, period="quarterly")

        series = self._series(client)
        assert series[self.MERCHANT]["period_label"] == "quarterly"
        listed = client.get(f"{PREFIX}/recurring/marked", params={"profile": "dummy"}).json()
        assert listed["total"] == 1

    def test_unmarking_something_never_marked_is_not_an_error(self, client):
        body = client.request("DELETE", f"{PREFIX}/recurring/mark", params={
            "profile": "dummy", "merchant": "NOTHING", "amount_centre_minor": 1,
        }).json()
        assert body["unmarked"] is False

    def test_a_mark_can_be_found_again(self, client, repository):
        """Contract rule 2a. A mark whose rows a reparse renamed stops
        appearing on the recurring page, and without this there would be
        nothing anywhere to say it still existed."""
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        self._mark(client)
        body = client.get(f"{PREFIX}/recurring/marked", params={"profile": "dummy"}).json()
        assert body["total"] == 1
        assert body["marked"][0]["merchant_norm"] == self.MERCHANT
        assert body["marked"][0]["period_label"] == "yearly"

    def test_an_invented_period_is_refused(self, client):
        assert self._mark(client, period="fortnightlyish").status_code == 422

    def test_a_mark_does_not_touch_the_transactions(self, client, repository):
        self._seed(repository, [date(2024, 6, 15), date(2025, 6, 15)])
        before = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy"},
        ).json()["transactions"]
        self._mark(client)
        after = client.get(
            f"{PREFIX}/transactions", params={"profile": "dummy"},
        ).json()["transactions"]
        assert [r["id"] for r in after] == [r["id"] for r in before]


class TestSeeingWhatAMarkWouldGather:
    """A mark reaches every comparable row, not the one that was clicked.

    Those are different things, and the difference is only visible before the
    mark is written — afterwards it is a monthly total nobody can account for.
    """

    MERCHANT = "SOME INSURER"

    def _seed(self, repository, rows):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="f" * 64, institution="Test", doc_type="acc",
                period_start=date(2023, 1, 1), period_end=date(2026, 12, 31),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="f.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=day, amount_minor=amount,
                    currency="SGD", description_raw=name, description_norm=name,
                    counterparty_norm=name, dedupe_key=f"c{index}", seq=index,
                )
                for index, (day, amount, name) in enumerate(rows)
            ],
        )

    def _candidates(self, client, txn_id, period="yearly"):
        return client.get(f"{PREFIX}/recurring/candidates", params={
            "profile": "dummy", "txn_id": txn_id, "period": period,
        })

    def test_it_shows_every_row_the_mark_would_take(self, client, repository):
        self._seed(repository, [
            (date(2024, 6, 15), -27386, self.MERCHANT),
            (date(2025, 6, 15), -27386, self.MERCHANT),
            (date(2025, 8, 1), -1200, "SOMEWHERE ELSE"),
        ])
        rows = client.get(f"{PREFIX}/transactions", params={"profile": "dummy"}).json()
        chosen = next(r for r in rows["transactions"] if r["amount_minor"] == -27386)

        body = self._candidates(client, chosen["id"]).json()
        assert len(body["matches"]) == 2
        assert body["merchant"] == self.MERCHANT
        assert body["gap_days"] == [365]

    def test_it_says_what_the_period_would_mean(self, client, repository):
        """The gaps beside the period is how somebody notices they picked
        monthly for something billed yearly."""
        self._seed(repository, [
            (date(2024, 6, 15), -27386, self.MERCHANT),
            (date(2025, 6, 15), -27386, self.MERCHANT),
        ])
        rows = client.get(f"{PREFIX}/transactions", params={"profile": "dummy"}).json()
        chosen = rows["transactions"][0]

        body = self._candidates(client, chosen["id"], period="monthly").json()
        assert body["expected_gap_days"] == 30
        assert body["gap_days"] == [365]
        assert body["monthly_equivalent_minor"] == 27386

    def test_nothing_is_written(self, client, repository):
        self._seed(repository, [(date(2024, 6, 15), -27386, self.MERCHANT)])
        rows = client.get(f"{PREFIX}/transactions", params={"profile": "dummy"}).json()
        self._candidates(client, rows["transactions"][0]["id"])
        listed = client.get(f"{PREFIX}/recurring/marked", params={"profile": "dummy"}).json()
        assert listed["total"] == 0

    def test_a_row_that_is_not_a_candidate_says_so(self, client, repository):
        """A transfer leg is deliberately not a recurrence candidate, and
        answering 'no such row' about one on screen would be a lie nobody can
        act on."""
        assert self._candidates(client, 999999).status_code == 404

    def test_an_invented_period_is_refused(self, client, repository):
        self._seed(repository, [(date(2024, 6, 15), -27386, self.MERCHANT)])
        rows = client.get(f"{PREFIX}/transactions", params={"profile": "dummy"}).json()
        response = self._candidates(client, rows["transactions"][0]["id"], period="often")
        assert response.status_code == 422


class TestRecategorisingASubscription:
    """A subscription filed wrongly has to be correctable where it is noticed.

    Through the same route the review queue uses, not a second one: a series
    *is* a merchant, a merchant's category is one decision, and two ways to set
    one thing is how they come to disagree.
    """

    def _seed_a_series(self, repository, counterparty="MONTHLY THING"):
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="b" * 64, institution="Test", doc_type="acc",
                period_start=date(2026, 1, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="s.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=date(2026, month, 5),
                    amount_minor=-2999, currency="SGD",
                    description_raw=counterparty, description_norm=counterparty,
                    counterparty_norm=counterparty, dedupe_key=f"sub{month}", seq=month,
                )
                for month in (3, 4, 5, 6)
            ],
        )

    def _series(self, client, merchant):
        body = client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()
        everything = body["series"] + body["lapsed"] + body["overdue"] + body["due_soon"]
        return next((s for s in everything if s["merchant"] == merchant), None)

    def test_a_series_says_what_it_is_filed_under(self, client, repository):
        self._seed_a_series(repository)
        found = self._series(client, "MONTHLY THING")
        assert found is not None, "four monthly payments should be a series"
        assert found["category"] is None and found["decided_by"] is None

    def test_correcting_it_reaches_the_ledger_and_the_page(self, client, repository):
        self._seed_a_series(repository)
        client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "MONTHLY THING",
                    "category": "Bills and utilities"},
        )
        found = self._series(client, "MONTHLY THING")
        assert found["category"] == "Bills and utilities"
        assert found["decided_by"] == "operator"

        rows = client.get(
            f"{PREFIX}/transactions",
            params={"profile": "dummy", "category": "Bills and utilities"},
        ).json()["transactions"]
        assert len(rows) == 4, "the correction covers the whole series, not one row"

    def test_a_series_with_no_payee_is_not_offered_as_a_decision(self, client, repository):
        """A series gathered by amount carries a label the server invented, not
        a counterparty any row holds — the bank printed no payee.

        Deciding that label writes a rule matching nothing, and the chip would
        then read the rule back and show the category as settled while no
        transaction had been touched. Saying there is nothing to file is worse
        for the operator only until they notice the alternative was a lie.
        """
        from datetime import date, datetime, timezone

        from app.domain.models import DEPOSIT
        from app.domain.recurrence import UNNAMED
        from app.ports.repository import AccountRecord, DocumentRecord, TxnRecord

        context = repository.resolve_context("default-dummy", "owner@localhost")
        account = AccountRecord(
            institution="Test", account_ref_masked="1", sub_account_label="",
            currency="SGD", kind=DEPOSIT,
        )
        rail = "GIRO PAYMENTS / COLLECTIONS VIA GIRO"
        repository.insert_document(
            context,
            DocumentRecord(
                sha256="f" * 64, institution="Test", doc_type="acc",
                period_start=date(2023, 1, 1), period_end=date(2026, 6, 30),
                storage_path="x", parse_status="imported",
                source_profile="dummy", source_relpath="f.pdf",
                fetched_at=datetime.now(timezone.utc),
            ),
            [],
            [
                TxnRecord(
                    account_key=account, posted_date=date(year, 3, 20),
                    amount_minor=-21525, currency="SGD",
                    description_raw=rail, description_norm=rail,
                    counterparty_norm=rail, dedupe_key=f"rail{year}", seq=year,
                )
                for year in (2024, 2025, 2026)
            ],
        )

        body = client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()
        everything = body["series"] + body["lapsed"] + body["overdue"] + body["due_soon"]
        found = next(s for s in everything if s["merchant"] == UNNAMED)

        assert found["grouped_by"] == "amount"
        assert found["category"] is None and found["decided_by"] is None

    def test_deciding_the_invented_label_never_reports_a_category(self, client, repository):
        """Even if a client tries it anyway, the series must not claim to be
        filed — nothing was."""
        from app.domain.recurrence import UNNAMED

        client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": UNNAMED, "category": "Insurance"},
        )
        body = client.get(f"{PREFIX}/recurring", params={"profile": "dummy"}).json()
        everything = body["series"] + body["lapsed"] + body["overdue"] + body["due_soon"]
        for series in everything:
            if series["merchant"] == UNNAMED:
                assert series["category"] is None

    def test_it_can_be_corrected_twice(self, client, repository):
        """A wrong category is exactly the thing a person fixes more than once."""
        self._seed_a_series(repository)
        for category in ("Bills and utilities", "Insurance", "Recreation"):
            client.post(
                f"{PREFIX}/review/decide",
                params={"profile": "dummy", "counterparty": "MONTHLY THING",
                        "category": category},
            )
            assert self._series(client, "MONTHLY THING")["category"] == category
