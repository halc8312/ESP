from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from urllib.parse import urlsplit, urlunsplit

# Renderの永続ディスクを利用する場合、そのパスを環境変数で指定します。
database_url = os.environ.get("DATABASE_URL", "sqlite:///mercari.db")

def _safe_database_label(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "configured"
    if parsed.password:
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return url


print(f"Using database URL: {_safe_database_label(database_url)}")

engine = create_engine(database_url, echo=False)

# SQLiteの場合のみWALモードを有効化
if "sqlite" in engine.url.drivername:
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(engine)
