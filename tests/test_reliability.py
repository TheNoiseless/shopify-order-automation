import csv
import json
import logging
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.logging_config import LOGGER_NAME
from app.main import create_app
from app.outputs import OutputGenerationError, generate_order_outputs


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


def test_incomplete_order_recovers_without_duplicate_rows(
    client: TestClient,
    database_path: Path,
    output_directory: Path,
    valid_order: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fail_once(order, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OutputGenerationError
        generate_order_outputs(order, destination)

    monkeypatch.setattr(main_module, "generate_order_outputs", fail_once)

    first_response = client.post("/webhooks/orders/create", json=valid_order)
    recovery_response = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        processing_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM line_items"
        ).fetchone()[0]

    csv_path = output_directory / "order_123456789_fulfillment.csv"
    pdf_path = output_directory / "order_123456789_packing_slip.pdf"

    assert first_response.status_code == 500
    assert recovery_response.status_code == 200
    assert recovery_response.json() == {
        "status": "recovered",
        "order_id": 123456789,
    }
    assert processing_status == "completed"
    assert order_count == 1
    assert item_count == 1
    assert csv_path.is_file()
    assert pdf_path.read_bytes().startswith(b"%PDF-")


def test_recovery_can_fail_again_then_succeed(
    client: TestClient,
    database_path: Path,
    valid_order: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fail_twice(order, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OutputGenerationError
        generate_order_outputs(order, destination)

    monkeypatch.setattr(main_module, "generate_order_outputs", fail_twice)

    first_response = client.post("/webhooks/orders/create", json=valid_order)
    failed_recovery = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        failed_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]
        failed_counts = (
            connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM line_items").fetchone()[0],
        )

    successful_recovery = client.post("/webhooks/orders/create", json=valid_order)

    with sqlite3.connect(database_path) as connection:
        completed_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]

    assert first_response.status_code == 500
    assert failed_recovery.status_code == 500
    assert failed_status == "output_failed"
    assert failed_counts == (1, 1)
    assert successful_recovery.status_code == 200
    assert successful_recovery.json()["status"] == "recovered"
    assert completed_status == "completed"


def test_conflicting_recovery_payload_uses_persisted_order(
    client: TestClient,
    database_path: Path,
    output_directory: Path,
    valid_order: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_outputs(*_args, **_kwargs) -> None:
        raise OutputGenerationError

    monkeypatch.setattr(main_module, "generate_order_outputs", fail_outputs)
    client.post("/webhooks/orders/create", json=valid_order)

    conflicting_order = deepcopy(valid_order)
    conflicting_order["total_price"] = "999.99"
    conflicting_order["line_items"][0]["sku"] = "CONFLICTING-SKU"
    conflicting_order["line_items"][0]["quantity"] = 99
    monkeypatch.setattr(
        main_module,
        "generate_order_outputs",
        generate_order_outputs,
    )

    response = client.post("/webhooks/orders/create", json=conflicting_order)

    with sqlite3.connect(database_path) as connection:
        persisted_total = connection.execute(
            "SELECT total_price FROM orders"
        ).fetchone()[0]
        persisted_item = connection.execute(
            "SELECT sku, quantity FROM line_items"
        ).fetchone()

    csv_path = output_directory / "order_123456789_fulfillment.csv"
    with csv_path.open(encoding="utf-8", newline="") as stream:
        csv_row = next(csv.DictReader(stream))

    assert response.status_code == 200
    assert response.json()["status"] == "recovered"
    assert persisted_total == "149.98"
    assert persisted_item == ("UMBRA-BLK", 2)
    assert csv_row["sku"] == "UMBRA-BLK"
    assert csv_row["quantity"] == "2"


def test_structured_events_are_capturable(
    client: TestClient,
    valid_order: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    application_logger = logging.getLogger(LOGGER_NAME)
    application_logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    recovery_order = deepcopy(valid_order)
    recovery_order["id"] = 987654321
    attempts = 0

    def fail_twice(order, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OutputGenerationError
        generate_order_outputs(order, destination)

    try:
        client.post("/webhooks/orders/create", json=valid_order)
        client.post("/webhooks/orders/create", json=valid_order)
        monkeypatch.setattr(main_module, "generate_order_outputs", fail_twice)
        client.post("/webhooks/orders/create", json=recovery_order)
        client.post("/webhooks/orders/create", json=recovery_order)
        client.post("/webhooks/orders/create", json=recovery_order)
    finally:
        application_logger.removeHandler(caplog.handler)

    events = {getattr(record, "event", None) for record in caplog.records}
    assert {
        "webhook_received",
        "order_accepted",
        "order_duplicate",
        "output_failed",
        "recovery_started",
        "recovery_failed",
        "recovery_completed",
    } <= events
    assert {getattr(record, "order_id", None) for record in caplog.records} == {
        123456789,
        987654321,
    }


def test_initialize_adds_status_to_existing_slice_c_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                created_at TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_price TEXT NOT NULL,
                customer_first_name TEXT NOT NULL,
                customer_last_name TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                123456789,
                "customer@example.com",
                "2026-09-23T12:00:00+00:00",
                "USD",
                "149.98",
                "John",
                "Doe",
            ),
        )

    with TestClient(create_app(database_path, tmp_path / "outputs")):
        pass

    with sqlite3.connect(database_path) as connection:
        processing_status = connection.execute(
            "SELECT processing_status FROM orders"
        ).fetchone()[0]

    assert processing_status == "pending"


def test_application_releases_sqlite_connections(
    tmp_path: Path,
    valid_order: dict,
) -> None:
    database_path = tmp_path / "releasable.db"
    with TestClient(create_app(database_path, tmp_path / "outputs")) as client:
        response = client.post("/webhooks/orders/create", json=valid_order)

    database_path.unlink()

    assert response.status_code == 202
    assert not database_path.exists()
