import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from greek_news import models


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as database:
        yield database
    engine.dispose()
