import asyncio
from datetime import time, timedelta

import pytest
import test_queue
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr
from sqlalchemy import func, select
from test_queue import TODAY, book, day
from test_web import TOKEN, init_data

from labdefender.db.models import DefenseSlot
from labdefender.handlers.notifications import Notifier
from labdefender.services.errors import DefenseSlotOverlap
from labdefender.web.app import create_web_app
from labdefender.web.idempotency import IdempotencyCache, InvalidIdempotencyKey

# Teacher 101 owns «ИВТ-101» with «ЛР 1»; students 202-204 are approved and not booked yet.
# Teacher 303 and student 404 have no groups. Moscow time is fixed at 2026-11-10 10:00.
context = test_queue.context
TEACHER, OTHER_TEACHER, STUDENT = 101, 303, 202
TOMORROW = TODAY + timedelta(days=1)


def auth(user):
    return {"Authorization": f"tma {init_data(user_id=user)}"}


@pytest.fixture
async def client(context, bot):
    sessions, *_ = context
    app = create_web_app(
        sessions,
        bot_token=SecretStr(TOKEN),
        auth_max_age=3600,
        bot=bot,
        notifier=Notifier(),
        storage=MemoryStorage(),
    )
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def call(client, method, path, *, user=TEACHER, status=200, **kwargs):
    headers = {**auth(user), **kwargs.pop("headers", {})}
    response = await client.request(method, f"/api{path}", headers=headers, **kwargs)
    body = await response.json()
    assert response.status == status, body
    return body


def notices(bot):
    return sorted(int(c.chat_id) for c in bot.session.calls if isinstance(c, SendMessage))


def draft(**changes):
    return {
        "date": TOMORROW.isoformat(),
        "start": "14:00",
        "end": "16:00",
        "capacity": 3,
        **changes,
    }


async def test_teacher_navigates_groups_and_assignments(context, client):
    _, _, group, assignment, _ = context
    groups = await call(client, "GET", "/groups")
    assert groups == {
        "items": [{"id": group.id, "name": "ИВТ-101"}],
        "offset": 0,
        "has_next": False,
    }
    listing = await call(client, "GET", f"/groups/{group.id}/assignments")
    assert listing["group"] == {"id": group.id, "name": "ИВТ-101"}
    assert listing["items"] == [
        {
            "id": assignment.id,
            "group_id": group.id,
            "title": "ЛР 1",
            "format": "online",
            "format_label": "онлайн",
        }
    ]
    assert (await call(client, "GET", "/groups", user=OTHER_TEACHER))["items"] == []
    await call(client, "GET", f"/groups/{group.id}/assignments", user=OTHER_TEACHER, status=404)
    await call(client, "GET", "/groups?offset=-1", status=422)
    await call(client, "GET", "/groups?offset=99999999999", status=422)


async def test_calendar_marks_dates_with_slots_and_pages_like_the_chat(context, client):
    _, _, _, assignment, _ = context
    await day(context, day_date=TOMORROW)
    calendar = await call(client, "GET", f"/groups/{context[2].id}/calendar")
    assert calendar["group"]["name"] == "ИВТ-101"
    assert (calendar["page"], calendar["leading"], calendar["weekdays"][0]) == (0, 1, "Пн")
    assert calendar["range_label"] == "10–28 ноября 2026"
    days = calendar["days"]
    assert (days[0]["date"], days[-1]["date"], len(days)) == ("2026-11-10", "2026-11-28", 19)
    assert days[0]["today"] and not days[1]["today"]
    assert [entry["date"] for entry in days if entry["marked"]] == [TOMORROW.isoformat()]
    second = await call(client, "GET", f"/groups/{context[2].id}/calendar?page=1")
    assert second["days"][0]["date"] == "2026-11-29"
    pages = calendar["pages"]
    await call(client, "GET", f"/groups/{context[2].id}/calendar?page={pages}", status=422)
    await call(client, "GET", f"/groups/{context[2].id}/calendar", user=STUDENT, status=403)
    await call(client, "GET", f"/groups/{context[2].id}/calendar", user=OTHER_TEACHER, status=404)


async def test_day_offers_free_future_starts_durations_and_template(context, client):
    _, _, _, assignment, _ = context
    today = await call(client, "GET", f"/groups/{context[2].id}/day?date={TODAY}")
    starts = [entry["time"] for entry in today["starts"]]
    assert starts[0] == "10:30" and starts[-1] == "20:30"
    assert today["template"] is None and today["busy"] == []
    assert today["capacities"] == [5, 10, 15, 20, 25, 30]

    await day(context, day_date=TOMORROW, start_time=time(14), end_time=time(16), capacity=7)
    tomorrow = await call(client, "GET", f"/groups/{context[2].id}/day?date={TOMORROW}")
    assert (tomorrow["date_label"], tomorrow["weekday"]) == ("11.11.2026", "ср")
    assert tomorrow["busy"] == [{"start": "14:00", "end": "16:00", "time_label": "14:00–16:00"}]
    starts = {entry["time"]: entry["durations"] for entry in tomorrow["starts"]}
    assert "14:00" not in starts and "15:30" not in starts
    assert [d["minutes"] for d in starts["13:00"]] == [30, 60]
    assert starts["13:00"][1] == {"minutes": 60, "end": "14:00", "label": "1 ч"}
    # The template does not fit the busy day, but fits the next one.
    assert tomorrow["template"] is None
    later = await call(
        client, "GET", f"/groups/{context[2].id}/day?date={TOMORROW + timedelta(days=1)}"
    )
    assert later["template"] == {
        "start": "14:00",
        "end": "16:00",
        "time_label": "14:00–16:00",
        "capacity": 7,
    }
    await call(client, "GET", f"/groups/{context[2].id}/day?date=2026-11-09", status=422)
    await call(client, "GET", f"/groups/{context[2].id}/day?date=soon", status=422)


async def test_created_slot_notifies_once_per_key_and_validates(context, client, bot):
    sessions, _, _, assignment, _ = context
    path = f"/groups/{context[2].id}/slots"
    created = await call(
        client, "POST", path, json=draft(), headers={"Idempotency-Key": "draft-key-1"}, status=201
    )
    slot = created["slot"]
    assert (slot["date"], slot["time_label"], slot["capacity"], slot["occupied"]) == (
        TOMORROW.isoformat(),
        "14:00–16:00",
        3,
        0,
    )
    assert (slot["is_open"], slot["is_past"]) == (True, False)
    assert notices(bot) == [202, 203, 204]
    # A double tap or a retry after a lost answer: same slot, no second notification.
    again = await call(
        client, "POST", path, json=draft(), headers={"Idempotency-Key": "draft-key-1"}, status=201
    )
    assert again == created and notices(bot) == [202, 203, 204]
    overlap = await call(client, "POST", path, json=draft(start="15:00", end="17:00"), status=409)
    assert overlap["error"] == str(DefenseSlotOverlap())
    for invalid in (
        draft(start="16:00", end="14:00"),
        draft(date="2026-11-09"),
        draft(capacity=0),
        draft(capacity="3"),
        draft(start="9:00"),
        {**draft(), "extra": 1},
    ):
        assert (
            "Укажите дату" in (await call(client, "POST", path, json=invalid, status=422))["error"]
        )
    await call(client, "POST", path, data="not json", status=422)
    await call(client, "POST", path, json=draft(), headers={"Idempotency-Key": "bad"}, status=422)
    await call(
        client, "POST", path, json=draft(start="18:00", end="19:00"), user=STUDENT, status=403
    )
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(DefenseSlot)) == 1


async def test_group_slots_and_unbooked_works(context, client):
    _, _, _, assignment, _ = context
    slot = await day(context, day_date=TOMORROW, capacity=2)
    await book(context, slot, student=203)
    listing = await call(client, "GET", f"/groups/{context[2].id}/slots")
    assert listing["group"] == {"id": context[2].id, "name": "ИВТ-101"}
    [item] = listing["items"]
    assert (item["id"], item["occupied"], item["capacity"]) == (slot.id, 1, 2)
    unassigned = await call(client, "GET", f"/groups/{context[2].id}/unassigned")
    assert [
        (entry["name"], entry["assignment_title"], entry["attempt_number"])
        for entry in unassigned["items"]
    ] == [("User202", "ЛР 1", 1), ("User204", "ЛР 1", 1)]
    await call(client, "GET", f"/groups/{context[2].id}/unassigned", user=STUDENT, status=403)


async def test_queue_close_reopen_and_cancel(context, client, bot):
    _, _, _, assignment, _ = context
    slot = await day(context, day_date=TOMORROW)
    await book(context, slot, student=204)
    await book(context, slot, student=202)
    queue = await call(client, "GET", f"/slots/{slot.id}/queue")
    assert queue["group"]["name"] == "ИВТ-101"
    assert [
        (m["position"], m["name"], m["assignment_title"], m["attempt_number"])
        for m in queue["items"]
    ] == [(1, "User202", "ЛР 1", 1), (2, "User204", "ЛР 1", 1)]
    assert queue["slot"]["occupied"] == 2

    closed = await call(client, "POST", f"/slots/{slot.id}/close")
    assert closed["slot"]["is_open"] is False
    reopened = await call(client, "POST", f"/slots/{slot.id}/reopen")
    assert (reopened["reopened"], reopened["slot"]["is_open"]) == (True, True)
    assert notices(bot) == [203]  # The only approved student without a booking.
    assert (await call(client, "POST", f"/slots/{slot.id}/reopen"))["reopened"] is False
    assert notices(bot) == [203]

    busy = await call(client, "DELETE", f"/slots/{slot.id}", status=409)
    assert "Нельзя отменить слот" in busy["error"]
    empty = await day(context, day_date=TOMORROW, start_time=time(17), end_time=time(18))
    assert await call(client, "DELETE", f"/slots/{empty.id}") == {
        "deleted": True,
        "group_id": context[2].id,
    }
    await call(client, "DELETE", f"/slots/{empty.id}", status=404)
    # A student has no teacher rights; another teacher does not even see the slot.
    for user, status in ((STUDENT, 403), (OTHER_TEACHER, 404)):
        await call(client, "GET", f"/slots/{slot.id}/queue", user=user, status=status)
        await call(client, "POST", f"/slots/{slot.id}/close", user=user, status=status)
        await call(client, "POST", f"/slots/{slot.id}/reopen", user=user, status=status)
    assert notices(bot) == [203]


async def test_unexpected_failure_is_logged_and_hidden(context, client, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr("labdefender.web.api.courses.list_groups", broken)
    body = await call(client, "GET", "/groups", status=500)
    assert body == {"error": "Не удалось выполнить действие. Попробуйте ещё раз."}
    assert "database exploded" in caplog.text


async def test_idempotency_runs_once_and_forgets_failures_and_old_keys():
    now = [0.0]
    cache = IdempotencyCache(ttl=10, limit=2, clock=lambda: now[0])
    calls = []

    async def action():
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"n": len(calls)}

    first, second = await asyncio.gather(
        cache.run(1, "key-0001", action), cache.run(1, "key-0001", action)
    )
    assert first == second == {"n": 1} and len(calls) == 1
    assert await cache.run(2, "key-0001", action) == {"n": 2}  # Keys are per user.
    assert await cache.run(1, None, action) == {"n": 3}  # No key: no protection.

    async def failing():
        calls.append(1)
        raise DefenseSlotOverlap()

    for _ in range(2):
        with pytest.raises(DefenseSlotOverlap):
            await cache.run(1, "key-0002", failing)
    assert len(calls) == 5
    now[0] = 11
    assert await cache.run(1, "key-0001", action) == {"n": 6}
    with pytest.raises(InvalidIdempotencyKey):
        await cache.run(1, "bad key", action)
