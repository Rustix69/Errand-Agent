"""Typed results returned by the mock marketplace tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

BookingStatus = Literal["confirmed", "completed", "cancelled", "cancelled_by_provider"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DateRange(_Model):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date_range.end is before date_range.start")
        return self


class ProviderSummary(_Model):
    provider_id: str
    name: str
    service: str
    areas: list[str]
    price: int  # INR, search-time estimate
    rating: float
    review_count: int
    available_slots: list[str]  # "YYYY-MM-DDTHH:MM", local IST, 1-hour slots
    attributes: dict[str, Any]


class SearchResult(_Model):
    service: str
    area: str
    date_range: DateRange
    providers: list[ProviderSummary]


class Review(_Model):
    author: str
    rating: int
    date: str
    text: str  # UNTRUSTED free text


class ProviderDetails(_Model):
    provider_id: str
    name: str
    service: str
    areas: list[str]
    price: int
    rating: float
    review_count: int
    working_hours: str
    working_days: list[str]
    attributes: dict[str, Any]
    about: str
    reviews: list[Review]


class Hold(_Model):
    hold_id: str
    provider_id: str
    slot: str
    search_price: int
    quoted_price: int
    price_changed: bool
    expires_at: datetime


class HoldInfo(_Model):
    hold_id: str
    provider_id: str
    slot: str
    quoted_price: int
    status: Literal["active", "expired", "booked", "released"]
    expires_at: datetime


class BookResult(_Model):
    booking_id: str
    hold_id: str
    provider_id: str
    slot: str
    price: int
    status: Literal["confirmed"]
    replayed: bool = False  # True when an idempotent retry returned the stored result


class BookingInfo(_Model):
    booking_id: str
    provider_id: str
    provider_name: str
    service: str
    slot: str
    price: int
    status: BookingStatus
    technician: str | None
    is_upcoming: bool
    cancel_reason: str | None = None


class BookingStatusResult(_Model):
    """Authoritative answer to 'did this booking happen?'."""

    status: Literal["confirmed", "completed", "cancelled", "cancelled_by_provider", "not_booked"]
    booking: BookingInfo | None
    hold: HoldInfo | None


class CancelResult(_Model):
    booking_id: str
    status: Literal["cancelled"]
    cancelled_at: datetime
    replayed: bool = False


class RescheduleResult(_Model):
    booking_id: str
    provider_id: str
    old_slot: str
    new_slot: str
    status: Literal["confirmed"]
    replayed: bool = False


class BookingList(_Model):
    user_id: str
    as_of: datetime
    bookings: list[BookingInfo]
