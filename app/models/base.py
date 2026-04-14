"""Declarative base with sarah schema."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all Sarah models."""

    __table_args__ = {"schema": "sarah"}
