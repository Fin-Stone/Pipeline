"""The HTTP surface, against the contract in docs/api-contracts.md.

These assert the promises a client is written against, not the shape of the
current implementation. A test here failing means somebody's install breaks.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="API extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import API_VERSION, PREFIX, app, _config  # noqa: E402


@pytest.fixture
def client(config, repository):
    """`repository` is depended on for its schema, not its handle: it builds
    the tables in the same database the API will open for itself."""
    app.dependency_overrides[_config] = lambda: config
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
