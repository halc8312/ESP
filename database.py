from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
import os

# Renderの永続ディスクを利用する場合、そのパスを環境変数で指定します。
database_url = os.environ.get("DATABASE_URL", "sqlite:///mercari.db")

print(f"DEBUG: Using database URL: {database_url}")

engine = create_engine(database_url, echo=False)

# SQLiteの場合のみWALモードを有効化
if "sqlite" in engine.url.drivername:
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

def init_db():
    Base.metadata.create_all(engine)
