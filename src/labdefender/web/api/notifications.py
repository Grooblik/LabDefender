"""The teacher's notification categories, as «🔔 Уведомления» in the chat."""

from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict

from labdefender.keyboards.notifications import CATEGORY_LABELS
from labdefender.services.notifications import (
    NoticeCategory,
    NotificationPreferences,
    get_notification_preferences,
    set_notification_preferences,
)
from labdefender.web.context import current_user, db, read_body

routes = web.RouteTableDef()


class Switch(BaseModel):
    model_config = ConfigDict(strict=True)
    category: Literal["submissions", "joins", "queue", "all"]
    enabled: bool


def preferences_json(preferences: NotificationPreferences) -> dict:
    return {
        "categories": [
            {
                "key": category.value,
                "label": label[:1].upper() + label[1:],
                "enabled": preferences.enabled(category),
            }
            for category, label in CATEGORY_LABELS.items()
        ]
    }


@routes.get("/api/notifications")
async def show(request: web.Request) -> web.Response:
    # Students always receive their notifications: the service answers them with 403.
    preferences = await get_notification_preferences(db(request), actor_id=current_user(request).id)
    return web.json_response(preferences_json(preferences))


@routes.put("/api/notifications")
async def switch(request: web.Request) -> web.Response:
    """Set one category or all of them; repeating the same request changes nothing."""
    body = await read_body(request, Switch)
    categories = list(NoticeCategory) if body.category == "all" else [NoticeCategory(body.category)]
    preferences = await set_notification_preferences(
        db(request),
        actor_id=current_user(request).id,
        categories=categories,
        enabled=body.enabled,
    )
    return web.json_response(preferences_json(preferences))
