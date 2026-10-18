"""Эндпоинт проверки живости (health)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Проверка здоровья",
    description="Проверка живости, которой подтверждают, что сервис работает.",
    responses={
        200: {
            "description": "Сервис работает.",
            "content": {
                "application/json": {
                    "example": {"status": "ok"},
                }
            },
        }
    },
)
def health() -> dict[str, str]:
    return {"status": "ok"}
