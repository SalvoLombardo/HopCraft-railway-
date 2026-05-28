#Conn to db

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass



# echo=True used for printing during development
_db_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _db_url,
    echo=(settings.app_env == "development"),
)


async_session_maker = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncSession:
    async with async_session_maker() as session:
        yield session
