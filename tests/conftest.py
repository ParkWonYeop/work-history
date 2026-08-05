from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from work_history.config import Settings
from work_history.models import Base


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        read_api_token="read-test-token",
        atlassian_site_url="https://example.atlassian.net",
        atlassian_email="person@example.com",
        atlassian_api_token="atlassian-test-token",
        public_base_url="https://history.example.com",
    )
