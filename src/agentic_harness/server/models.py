from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    agent = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    tool_call_id = Column(String, default="")
    timestamp = Column(String, nullable=False)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    data = Column(Text, nullable=False)
    timestamp = Column(String, nullable=False)


class Orchestration(Base):
    __tablename__ = "orchestrations"

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=False, index=True)
    thread_id = Column(String, nullable=False)
    steps = Column(Text, nullable=False)
    results = Column(Text, nullable=False)
    created_at = Column(String, nullable=False)


def create_database(database_path: str = "harness.db") -> tuple:
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return engine, session_factory
