"""MetriLens's own SQLite database, separate from any METRION database.

Defaults to `data/metrilens.db`, relative to the working directory.
"""

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from metrilens.models import Base

DEFAULT_DB_PATH = Path("data/metrilens.db")


def get_engine(db_path: Path = DEFAULT_DB_PATH) -> Engine:
    """Create the engine for `db_path`, creating its directory and tables if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def get_session(engine: Engine) -> Session:
    """Open a new session on `engine`."""
    return Session(engine)
