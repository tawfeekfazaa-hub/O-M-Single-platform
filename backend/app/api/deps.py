"""FastAPI dependencies.

The API layer depends on the repository only — never on vendor adapters
(CLAUDE.md rule 2: no ad-hoc vendor API calls from serving paths).
"""

from __future__ import annotations

from fastapi import Request

from app.repositories.base import Repository


def get_repository(request: Request) -> Repository:
    return request.app.state.repository
