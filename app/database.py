import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from app.models import ShopifyOrder


ProcessingStatus = Literal["pending", "completed", "output_failed"]


@dataclass(frozen=True)
class StoredOrder:
    order: ShopifyOrder
    processing_status: ProcessingStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3),
    total_price TEXT NOT NULL,
    customer_first_name TEXT NOT NULL,
    customer_last_name TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN ('pending', 'completed', 'output_failed'))
);

CREATE TABLE IF NOT EXISTS line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    title TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE CASCADE
);
"""


class OrderRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(orders)").fetchall()
            }
            if "processing_status" not in columns:
                connection.execute(
                    """
                    ALTER TABLE orders
                    ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (
                            processing_status IN (
                                'pending',
                                'completed',
                                'output_failed'
                            )
                        )
                    """
                )

    def create_or_get(self, order: ShopifyOrder) -> tuple[bool, StoredOrder]:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO orders (
                    order_id,
                    email,
                    created_at,
                    currency,
                    total_price,
                    customer_first_name,
                    customer_last_name,
                    processing_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(order_id) DO NOTHING
                """,
                (
                    order.id,
                    str(order.email),
                    order.created_at.isoformat(),
                    order.currency,
                    str(order.total_price),
                    order.customer.first_name,
                    order.customer.last_name,
                ),
            )

            created = cursor.rowcount == 1
            if created:
                connection.executemany(
                    """
                    INSERT INTO line_items (
                        order_id,
                        sku,
                        title,
                        quantity,
                        price
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            order.id,
                            item.sku,
                            item.title,
                            item.quantity,
                            str(item.price),
                        )
                        for item in order.line_items
                    ),
                )

            return created, self._load(connection, order.id)

    def set_processing_status(
        self,
        order_id: int,
        processing_status: ProcessingStatus,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE orders
                SET processing_status = ?
                WHERE order_id = ?
                """,
                (processing_status, order_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError(f"Order {order_id} does not exist")

    def _load(self, connection: sqlite3.Connection, order_id: int) -> StoredOrder:
        order_row = connection.execute(
            """
            SELECT
                order_id,
                email,
                created_at,
                currency,
                total_price,
                customer_first_name,
                customer_last_name,
                processing_status
            FROM orders
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        if order_row is None:
            raise sqlite3.IntegrityError(f"Order {order_id} does not exist")

        item_rows = connection.execute(
            """
            SELECT sku, title, quantity, price
            FROM line_items
            WHERE order_id = ?
            ORDER BY id
            """,
            (order_id,),
        ).fetchall()
        order = ShopifyOrder.model_validate(
            {
                "id": order_row["order_id"],
                "email": order_row["email"],
                "created_at": order_row["created_at"],
                "currency": order_row["currency"],
                "total_price": order_row["total_price"],
                "customer": {
                    "first_name": order_row["customer_first_name"],
                    "last_name": order_row["customer_last_name"],
                },
                "line_items": [
                    {
                        "sku": row["sku"],
                        "title": row["title"],
                        "quantity": row["quantity"],
                        "price": row["price"],
                    }
                    for row in item_rows
                ],
            }
        )
        return StoredOrder(
            order=order,
            processing_status=cast(
                ProcessingStatus,
                order_row["processing_status"],
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
