"""The HTTP surface, against the contract in docs/api-contracts.md.

These assert the promises a client is written against, not the shape of the
current implementation. A test here failing means somebody's install breaks.
"""

from __future__ import annotations

from dataclasses import replace

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

    def test_deciding_is_recorded_but_not_applied(self, client):
        """Applying is a separate pass, so a run of decisions costs one write
        rather than one each."""
        body = client.post(
            f"{PREFIX}/review/decide",
            params={"profile": "dummy", "counterparty": "SOME SHOP", "category": "Grocery"},
        ).json()
        assert body["created"] is True and body["applied"] is False

    def test_deciding_twice_is_not_an_error(self, client):
        params = {"profile": "dummy", "counterparty": "TWICE", "category": "Dining"}
        client.post(f"{PREFIX}/review/decide", params=params)
        again = client.post(f"{PREFIX}/review/decide", params=params).json()
        assert again["created"] is False


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
