# Importare tutti i modelli qui serve a "registrarli" con Base.
# SQLAlchemy deve conoscere tutte le tabelle prima di poter
# chiamare create_all() o generare migrazioni Alembic.
from app.models.airport import Airport  # noqa: F401
from app.models.flight_cache import FlightCache  # noqa: F401
