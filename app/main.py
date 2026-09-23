import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, status

from app.database import OrderRepository
from app.logging_config import configure_logging, log_event
from app.models import ShopifyOrder, WebhookAccepted
from app.outputs import OutputGenerationError, generate_order_outputs


DEFAULT_DATABASE_PATH = Path("shopify_orders.db")
DEFAULT_OUTPUT_DIRECTORY = Path("outputs")
logger = configure_logging()


def create_app(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
) -> FastAPI:
    repository = OrderRepository(database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        repository.initialize()
        yield

    application = FastAPI(
        title="Shopify Order Automation",
        version="0.4.0",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/webhooks/orders/create",
        response_model=WebhookAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def receive_order_created(
        order: ShopifyOrder,
        response: Response,
    ) -> WebhookAccepted:
        log_event(
            logger,
            "webhook_received",
            "received",
            order_id=order.id,
        )
        try:
            created, stored_order = repository.create_or_get(order)
        except sqlite3.Error as error:
            log_event(
                logger,
                "persistence_failed",
                "error",
                order_id=order.id,
                error=error,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Order could not be persisted",
            ) from error

        if not created and stored_order.processing_status == "completed":
            log_event(
                logger,
                "order_duplicate",
                "completed",
                order_id=order.id,
            )
            response.status_code = status.HTTP_200_OK
            return WebhookAccepted(status="duplicate", order_id=order.id)

        recovering = not created
        if recovering:
            log_event(
                logger,
                "recovery_started",
                stored_order.processing_status,
                order_id=order.id,
            )

        try:
            generate_order_outputs(stored_order.order, output_directory)
        except OutputGenerationError as error:
            try:
                repository.set_processing_status(order.id, "output_failed")
            except sqlite3.Error as state_error:
                log_event(
                    logger,
                    "processing_state_update_failed",
                    "incomplete",
                    order_id=order.id,
                    error=state_error,
                )
            log_event(
                logger,
                "recovery_failed" if recovering else "output_failed",
                "output_failed",
                order_id=order.id,
                error=error,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Order persisted but output generation failed",
            ) from error

        try:
            repository.set_processing_status(order.id, "completed")
        except sqlite3.Error as error:
            log_event(
                logger,
                "processing_state_update_failed",
                "incomplete",
                order_id=order.id,
                error=error,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Order processing state could not be updated",
            ) from error

        if recovering:
            log_event(
                logger,
                "recovery_completed",
                "completed",
                order_id=order.id,
            )
            response.status_code = status.HTTP_200_OK
            return WebhookAccepted(status="recovered", order_id=order.id)

        log_event(
            logger,
            "order_accepted",
            "completed",
            order_id=order.id,
        )
        return WebhookAccepted(status="accepted", order_id=order.id)

    return application


app = create_app()
