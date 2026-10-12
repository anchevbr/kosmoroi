"""Bootstrap, inspect and run the automatic backend without requiring a news API."""

import argparse
import json
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from greek_news.config import get_settings
from greek_news.db import engine_for
from greek_news.discovery import import_catalog
from greek_news.gazetteer import bootstrap_geography
from greek_news.geography import RegisterInput, import_register
from greek_news.jobs import run_once
from greek_news.models import Article, ArticleVersion, PublicationRecord
from greek_news.operations import diagnostics
from greek_news.publishing import LocalPublisher
from greek_news.seeds import seed_candidates, seed_demo


def main():
    parser = argparse.ArgumentParser(prog="greek-news")
    commands = parser.add_subparsers(dest="command", required=True)
    geo = commands.add_parser("import-geo", help="Import a versioned country/region/city/municipality JSON register")
    geo.add_argument("path", type=Path)
    commands.add_parser("bootstrap", help="Import offline real geography and HTML source catalog")
    commands.add_parser("doctor", help="Report actual prerequisites and runtime readiness; exits 1 when unready")
    commands.add_parser("seed-candidates", help="Add sample inactive HTML candidates")
    commands.add_parser("seed-demo", help="Explicitly fictional offline scenarios")
    tick = commands.add_parser("run-once", help="Process due durable HTML, AI and publication jobs")
    tick.add_argument("--no-schedule", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    engine = engine_for(settings.database_url)
    if args.command == "run-once":
        print(json.dumps(run_once(sessionmaker(engine, expire_on_commit=False), settings, schedule=not args.no_schedule)))
        return
    if args.command == "doctor":
        with Session(engine) as session:
            result = diagnostics(session, settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["ready"] else 1)
    with Session(engine) as session, session.begin():
        if args.command == "bootstrap":
            result = {"imported_places": bootstrap_geography(session),
                      "imported_html_sources": import_catalog(session, settings.source_catalog_path, settings)}
            # Index old published versions using their own snapshots, never mutable story geography.
            backfilled = 0
            from greek_news.models import ArticleFacet
            for article in session.scalars(select(Article).where(Article.status == "published")):
                if session.scalar(select(ArticleFacet.id).where(ArticleFacet.article_id == article.id).limit(1)):
                    continue
                version = session.scalar(select(ArticleVersion).where(ArticleVersion.article_id == article.id,
                                          ArticleVersion.number == article.published_version))
                if not version or not version.evidence_packet:
                    continue
                old_status, old_approved = version.status, article.approved_version
                version.status, article.approved_version = "approved", version.number
                temp = PublicationRecord(article_id=article.id, version=version.number, state="pending")
                LocalPublisher().publish(session, temp, article.modified_at or article.published_at)
                version.status, article.approved_version = old_status, old_approved
                backfilled += 1
            result["backfilled_publications"] = backfilled
        elif args.command == "import-geo":
            content = RegisterInput.model_validate_json(args.path.read_text(encoding="utf-8"))
            result = {"imported_places": import_register(session, content), "register": content.id}
        elif args.command == "seed-candidates":
            result = {"added_candidates": seed_candidates(session)}
        else:
            if not settings.fixture_mode:
                raise SystemExit("seed-demo requires NEWS_FIXTURE_MODE=true; production never seeds fictional news")
            result = seed_demo(session)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
