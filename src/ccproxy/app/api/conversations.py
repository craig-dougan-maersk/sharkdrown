"""Conversation history endpoints."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ccproxy.app.conversations.reader import Conversation, ConversationMessage, ConversationReader

logger = logging.getLogger("claude_code_proxy.api.conversations")

router = APIRouter(prefix="/api")


@router.get("/conversations")
async def list_conversations(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
) -> JSONResponse:
    """List all conversations across all projects, sorted by start_time DESC."""
    base_path = _base_path(request)
    reader = ConversationReader(base_path=base_path)
    rows, total = reader.list_conversations(page=page, limit=limit)
    return JSONResponse(
        {
            "conversations": [_conversation_summary(r) for r in rows],
            "total": total,
            "page": page,
            "limit": limit,
        }
    )


@router.get("/conversations/project")
async def list_conversations_by_project(
    request: Request,
    project: str = Query(..., description="Project path segment under .claude/projects/"),
) -> JSONResponse:
    """All sessions under one project directory."""
    base_path = _base_path(request)
    reader = ConversationReader(base_path=base_path)
    rows = reader.get_conversations_by_project(project)
    return JSONResponse(
        {
            "project": project,
            "conversations": [_conversation_summary(r) for r in rows],
        }
    )


@router.get("/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    request: Request,
    project: str = Query(..., description="Project path segment"),
) -> JSONResponse:
    """One full conversation (every message)."""
    base_path = _base_path(request)
    reader = ConversationReader(base_path=base_path)
    conv = reader.get_conversation(session_id, project)
    if conv is None:
        return JSONResponse(
            {"error": f"Conversation {session_id!r} not found in project {project!r}"},
            status_code=404,
        )
    return JSONResponse(_conversation_full(conv))


def _base_path(request: Request) -> Path | None:
    """Default base_path; tests inject one via app.state.conversations_base_path."""
    override = getattr(request.app.state, "conversations_base_path", None)
    if override is not None:
        return Path(override)
    return None


def _conversation_summary(conv: Conversation) -> dict[str, object]:
    """Lightweight summary (no messages)."""
    return {
        "session_id": conv.session_id,
        "project_path": conv.project_path,
        "project_name": conv.project_name,
        "start_time": conv.start_time.isoformat() if conv.start_time else None,
        "end_time": conv.end_time.isoformat() if conv.end_time else None,
        "message_count": conv.message_count,
    }


def _conversation_full(conv: Conversation) -> dict[str, object]:
    """Full payload incl. every message."""
    return {
        **_conversation_summary(conv),
        "messages": [_message_to_json(m) for m in conv.messages],
    }


def _message_to_json(m: ConversationMessage) -> dict[str, object]:
    return {
        "role": m.role,
        "content": m.content,
        "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        "type": m.type,
    }
