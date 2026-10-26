"""Order storage behind a small interface: memory for tests, PostgreSQL for real."""

from typing import Protocol
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .models import Order


class OrderRepository(Protocol):
    def save(self, order: Order) -> None: ...
    def get(self, order_id: UUID) -> Order | None: ...
    def list_recent(self, limit: int) -> list[Order]: ...
    def ping(self) -> bool: ...


class InMemoryOrderRepository:
    def __init__(self) -> None:
        self._orders: dict[UUID, Order] = {}

    def save(self, order: Order) -> None:
        self._orders[order.id] = order

    def get(self, order_id: UUID) -> Order | None:
        return self._orders.get(order_id)

    def list_recent(self, limit: int) -> list[Order]:
        return sorted(self._orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]

    def ping(self) -> bool:
        return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          UUID PRIMARY KEY,
    account_id  TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    side        TEXT        NOT NULL,
    quantity    INTEGER     NOT NULL,
    price       NUMERIC(18,4) NOT NULL,
    status      TEXT        NOT NULL,
    reason      TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_created_at_idx ON orders (created_at DESC);
"""


class PostgresOrderRepository:
    """Uses a small connection pool; queries are parameterised (never string-formatted)."""

    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True, timeout=5)
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)

    def save(self, order: Order) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO orders (id, account_id, symbol, side, quantity, price, status, reason,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    order.id,
                    order.account_id,
                    order.symbol,
                    order.side.value,
                    order.quantity,
                    order.price,
                    order.status.value,
                    order.reason,
                    order.created_at,
                ),
            )

    def get(self, order_id: UUID) -> Order | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,)).fetchone()
        return Order(**row) if row else None

    def list_recent(self, limit: int) -> list[Order]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [Order(**r) for r in rows]

    def ping(self) -> bool:
        try:
            with self._pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False
