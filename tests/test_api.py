"""The HTTP surface, against the contract in docs/api-contracts.md.

These assert the promises a client is written against, not the shape of the
current implementation. A test here failing means somebody's install breaks.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("fastapi", reason="API extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import API_VERSION, PREFIX, app, _config  # noqa: E402


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
    with TestClient(app) as c:
        yield c
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
