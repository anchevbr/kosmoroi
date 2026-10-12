"""FastAPI composition: public reads and authenticated operator workflows."""

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from greek_news import admin, public
from greek_news.config import get_settings
from greek_news.operations import diagnostics
from greek_news.db import get_session


def create_app() -> FastAPI:
    app = FastAPI(title="Kosmoroi News Backend", version="0.2.0",
                  description="Automatic Greek and worldwide news from HTML with AI verification.")
    app.include_router(public.router)
    app.include_router(admin.router)

    @app.get("/health", tags=["operations"])
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready", tags=["operations"])
    def ready(session: Session = Depends(get_session), settings=Depends(get_settings)) -> dict:
        result = diagnostics(session, settings)
        if not result["ready"]:
            raise HTTPException(status_code=503, detail=result)
        return result

    return app


app = create_app()