from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from celine.onboarding.config.settings import settings

# A pooled connection can be closed while it sits idle (the server restarts, or
# something on the network path drops it), and the pool would hand it out
# unchecked: the first request after an idle stretch then fails. The pre-ping
# costs one round trip per checkout and replaces a dead connection. No
# pool_recycle: no idle timeout we know of applies to an idle pooled connection.
ENGINE_OPTIONS = {"echo": False, "pool_pre_ping": True}

engine = create_async_engine(settings.database_url, **ENGINE_OPTIONS)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
