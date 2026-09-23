from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
)


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Z]{3}$"),
]
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class Customer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: NonEmptyString
    last_name: NonEmptyString


class LineItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: NonEmptyString
    title: NonEmptyString
    quantity: int = Field(gt=0)
    price: Money


class ShopifyOrder(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: int = Field(gt=0)
    email: EmailStr
    created_at: AwareDatetime
    currency: CurrencyCode
    total_price: Money
    customer: Customer
    line_items: list[LineItem] = Field(min_length=1)


class WebhookAccepted(BaseModel):
    status: Literal["accepted", "duplicate", "recovered"]
    order_id: int
