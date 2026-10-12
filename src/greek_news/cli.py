"""Explicit migration-independent fixture and geographic import commands."""

import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from greek_news.config import get_settings
from greek_news.db import engine_for
from greek_news.geography import RegisterInput, import_register
from greek_news.jobs import run_once
from greek_news.seeds import seed_candidates, seed_demo


def main() -> None:
    parser = argparse.ArgumentParser(prog="greek-news")
    commands = parser.add_subparsers(dest="command", required=True)
    geography = commands.add_parser("import-geo", help="Import a vetted versioned JSON register")
    geography.add_argument("path", type=Path)
    commands.add_parser("seed-candidates", help="Add disabled source candidates")
    commands.add_parser("seed-demo", help="Import fictional offline scenarios")
    tick = commands.add_parser("run-once", help="Schedule, recover and process due jobs once")
    tick.add_argument("--no-schedule", action="store_true", help="Only process queued jobs")
    args = parser.parse_args()
    settings = get_settings()
    engine = engine_for(settings.database_url)
    if args.command == "run-once":
        factory = sessionmaker(engine, expire_on_commit=False)
        print(json.dumps(run_once(factory, settings, schedule=not args.no_schedule)))
        return
    with Session(engine) as session, session.begin():
        if args.command == "import-geo":
            content = RegisterInput.model_validate_json(args.path.read_text(encoding="utf-8"))
            result = {"imported_places": import_register(session, content),
                      "register": content.id, "fixture": content.is_fixture}
        elif args.command == "seed-candidates":
            result = {"added_inactive_candidates": seed_candidates(session)}
        else:
            result = seed_demo(session)
    print(json.dumps(result))


if __name__ == "__main__":
    main()