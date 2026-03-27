"""
database/db.py — SQLAlchemy engine, session factory, and DB initialisation.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from database.models import Base
from config import DB_URL, DB_PATH


# Ensure the data/ directory exists before SQLite tries to create the file
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},  # needed for SQLite + async use
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(engine)


def get_db() -> Session:
    """Return a new database session. Caller is responsible for closing it."""
    return SessionLocal()


def get_or_create_user(db: Session, chat_id: str):
    """Fetch the UserState row for chat_id, creating it if missing."""
    from database.models import UserState
    user = db.query(UserState).filter(UserState.telegram_chat_id == str(chat_id)).first()
    if not user:
        user = UserState(telegram_chat_id=str(chat_id))
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
