import csv
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import create_app
from app.outputs import OutputGenerationError


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "shopify_order.json"


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "orders.db"


@pytest.fixture
def output_directory(tmp_path: Path) -> Path:
    return tmp_path / "outputs"


@pytest.fixture
def client(
    database_path: Path,
    output_directory: Path,
) -> Iterator[TestClient]:
    with TestClient(
        create_app(database_path, output_directory),
    ) as test_client:
        yield test_client


@pytest.fixture
def valid_order() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_valid_webhook_is_accepted(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
) -> None:
    response = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        processing_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "order_id": 123456789,
    }
    assert processing_status == "completed"


def test_missing_required_field_is_rejected(
    client: TestClient,
    valid_order: dict,
) -> None:
    invalid_order = deepcopy(valid_order)
    del invalid_order["email"]

    response = client.post("/webhooks/orders/create", json=invalid_order)

    assert response.status_code == 422


def test_invalid_nested_line_item_is_rejected(
    client: TestClient,
    valid_order: dict,
) -> None:
    invalid_order = deepcopy(valid_order)
    invalid_order["line_items"][0]["quantity"] = 0

    response = client.post("/webhooks/orders/create", json=invalid_order)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "line_items", 0, "quantity"]


def test_empty_line_items_is_rejected(
    client: TestClient,
    valid_order: dict,
) -> None:
    invalid_order = deepcopy(valid_order)
    invalid_order["line_items"] = []

    response = client.post("/webhooks/orders/create", json=invalid_order)

    assert response.status_code == 422


def test_timestamp_without_timezone_is_rejected(
    client: TestClient,
    valid_order: dict,
) -> None:
    invalid_order = deepcopy(valid_order)
    invalid_order["created_at"] = "2026-09-23T12:00:00"

    response = client.post("/webhooks/orders/create", json=invalid_order)

    assert response.status_code == 422


def test_order_and_line_items_are_persisted(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
) -> None:
    client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        order = connection.execute(
            """
            SELECT
                order_id,
                email,
                created_at,
                currency,
                total_price,
                customer_first_name,
                customer_last_name
            FROM orders
            """
        ).fetchone()
        line_item = connection.execute(
            """
            SELECT order_id, sku, title, quantity, price
            FROM line_items
            """
        ).fetchone()

    assert order == (
        123456789,
        "customer@example.com",
        "2026-09-23T12:00:00+00:00",
        "USD",
        "149.98",
        "John",
        "Doe",
    )
    assert line_item == (
        123456789,
        "UMBRA-BLK",
        "Umbra Black",
        2,
        "74.99",
    )


def test_duplicate_order_is_reported_without_duplicate_rows(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
) -> None:
    first_response = client.post("/webhooks/orders/create", json=valid_order)
    duplicate_response = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        processing_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM line_items"
        ).fetchone()[0]

    assert first_response.status_code == 202
    assert duplicate_response.status_code == 200
    assert duplicate_response.json() == {
        "status": "duplicate",
        "order_id": 123456789,
    }
    assert order_count == 1
    assert processing_status == "completed"
    assert item_count == 1


def test_different_order_is_persisted(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
) -> None:
    second_order = deepcopy(valid_order)
    second_order["id"] = 987654321

    client.post("/webhooks/orders/create", json=valid_order)
    response = client.post("/webhooks/orders/create", json=second_order)

    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM line_items"
        ).fetchone()[0]

    assert response.status_code == 202
    assert response.json()["order_id"] == 987654321
    assert order_count == 2
    assert item_count == 2


def test_database_is_isolated_between_app_instances(
    tmp_path: Path,
    valid_order: dict,
) -> None:
    first_database = tmp_path / "first.db"
    second_database = tmp_path / "second.db"

    with TestClient(create_app(first_database, tmp_path / "first_outputs")) as first_client:
        first_client.post("/webhooks/orders/create", json=valid_order)

    with TestClient(create_app(second_database, tmp_path / "second_outputs")):
        pass

    with sqlite3.connect(first_database) as connection:
        first_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    with sqlite3.connect(second_database) as connection:
        second_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    assert first_count == 1
    assert second_count == 0


def test_line_item_failure_rolls_back_order(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_line_item
            BEFORE INSERT ON line_items
            BEGIN
                SELECT RAISE(ABORT, 'forced line item failure');
            END
            """
        )

    response = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    assert response.status_code == 503
    assert response.json() == {"detail": "Order could not be persisted"}
    assert order_count == 0


def test_new_order_generates_fulfillment_csv(
    client: TestClient,
    output_directory: Path,
    valid_order: dict,
) -> None:
    response = client.post("/webhooks/orders/create", json=valid_order)
    csv_path = output_directory / "order_123456789_fulfillment.csv"

    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert response.status_code == 202
    assert rows == [
        {
            "order_id": "123456789",
            "customer_name": "John Doe",
            "email": "customer@example.com",
            "sku": "UMBRA-BLK",
            "product_title": "Umbra Black",
            "quantity": "2",
            "unit_price": "74.99",
            "currency": "USD",
        }
    ]


def test_csv_contains_one_row_per_line_item(
    client: TestClient,
    output_directory: Path,
    valid_order: dict,
) -> None:
    valid_order["line_items"].append(
        {
            "sku": "AURORA-WHT",
            "title": "Aurora White",
            "quantity": 1,
            "price": "25.00",
        }
    )

    client.post("/webhooks/orders/create", json=valid_order)

    csv_path = output_directory / "order_123456789_fulfillment.csv"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))

    assert [row["sku"] for row in rows] == ["UMBRA-BLK", "AURORA-WHT"]
    assert len(rows) == 2


def test_new_order_generates_valid_pdf(
    client: TestClient,
    output_directory: Path,
    valid_order: dict,
) -> None:
    response = client.post("/webhooks/orders/create", json=valid_order)
    pdf_path = output_directory / "order_123456789_packing_slip.pdf"
    pdf_data = pdf_path.read_bytes()

    assert response.status_code == 202
    assert len(pdf_data) > 100
    assert pdf_data.startswith(b"%PDF-")
    assert pdf_data.rstrip().endswith(b"%%EOF")


def test_duplicate_does_not_regenerate_outputs(
    client: TestClient,
    output_directory: Path,
    valid_order: dict,
) -> None:
    client.post("/webhooks/orders/create", json=valid_order)
    csv_path = output_directory / "order_123456789_fulfillment.csv"
    pdf_path = output_directory / "order_123456789_packing_slip.pdf"
    csv_sentinel = b"do-not-replace-csv"
    pdf_sentinel = b"do-not-replace-pdf"
    csv_path.write_bytes(csv_sentinel)
    pdf_path.write_bytes(pdf_sentinel)

    response = client.post("/webhooks/orders/create", json=valid_order)

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert csv_path.read_bytes() == csv_sentinel
    assert pdf_path.read_bytes() == pdf_sentinel


def test_second_order_generates_independent_files(
    client: TestClient,
    output_directory: Path,
    valid_order: dict,
) -> None:
    second_order = deepcopy(valid_order)
    second_order["id"] = 987654321

    client.post("/webhooks/orders/create", json=valid_order)
    response = client.post("/webhooks/orders/create", json=second_order)

    assert response.status_code == 202
    assert sorted(path.name for path in output_directory.iterdir()) == [
        "order_123456789_fulfillment.csv",
        "order_123456789_packing_slip.pdf",
        "order_987654321_fulfillment.csv",
        "order_987654321_packing_slip.pdf",
    ]


def test_output_failure_keeps_order_and_returns_clear_error(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_outputs(*_args, **_kwargs) -> None:
        raise OutputGenerationError

    monkeypatch.setattr(main_module, "generate_order_outputs", fail_outputs)

    response = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        processing_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Order persisted but output generation failed",
    }
    assert order_count == 1
    assert processing_status == "output_failed"
