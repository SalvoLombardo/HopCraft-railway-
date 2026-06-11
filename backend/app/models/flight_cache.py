from datetime import date, datetime

from sqlalchemy import Boolean, Date, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.database import Base


class FlightCache(Base):
    __tablename__ = "flight_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin: Mapped[str] = mapped_column(String(3), nullable=False)
    destination: Mapped[str] = mapped_column(String(3), nullable=False)
    departure_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_eur: Mapped[float | None] = mapped_column(Numeric(10, 2))
    airline: Mapped[str | None] = mapped_column(String(100))
    direct_flight: Mapped[bool | None] = mapped_column(Boolean)
    flight_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    raw_response: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("origin", "destination", "departure_date"),
        Index("idx_cache_lookup", "destination", "departure_date", "fetched_at"),
        Index("idx_cache_expiry", "fetched_at"),
    )
