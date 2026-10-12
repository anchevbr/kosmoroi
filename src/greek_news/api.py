"""FastAPI composition: public reads and authenticated operator workflows."""

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from greek_news import admin, public
from greek_news.db import get_session


def create_app() -> FastAPI:
    app = FastAPI(title="Greek News Backend", version="0.1.0",
                  description="Permission-gated Greek news API; draft-only by default.")
    app.include_router(public.router)
    app.include_router(admin.router)

    @app.get("/health", tags=["operations"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", tags=["operations"])
    def ready(session: Session = Depends(get_session)) -> dict:
        try:
            revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Database migrations not ready") from exc
        return {"status": "ready", "database_revision": revision}

    return app


app = create_app()