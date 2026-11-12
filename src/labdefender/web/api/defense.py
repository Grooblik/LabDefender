"""Defense queue in the Mini App: slots of a group (discipline), one place per approved work.

Teacher: calendar, day grid, creation and the slot queue. Student: approved works of a group,
booking a work into a slot or switching it, moving to the end. The same services as the chat
check rights and validate; the grid rules come from `services.slot_grid`, so the page computes
no time itself.
"""

from datetime import date, datetime, time, timedelta

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from labdefender.db.models import DefenseSlot
from labdefender.handlers.notifications import notify_queue_booking, notify_slot_opened
from labdefender.services import queue as queue_service
from labdefender.services.assignments import get_assignment
from labdefender.services.errors import InvalidDefenseSlot
from labdefender.services.groups import get_group
from labdefender.services.queue import (
    cancel_defense_slot,
    close_defense_slot,
    create_defense_slot,
    enroll_slot,
    get_defense_slot,
    get_slot_queue,
    get_student_booking,
    latest_slot_template,
    list_approved_unassigned,
    list_defense_slots,
    list_defense_works,
    list_slots_between,
    move_to_end,
    reopen_defense_slot,
    require_approved,
)
from labdefender.services.slot_grid import (
    CAPACITIES,
    WEEKDAYS,
    calendar_pages,
    duration_label,
    duration_options,
    from_minutes,
    interval_label,
    range_label,
    start_options,
    template_fits,
    weekday_label,
)
from labdefender.web.api.courses import assignment_json
from labdefender.web.context import (
    BOT,
    IDEMPOTENCY,
    NOTIFIER,
    InvalidRequest,
    current_user,
    db,
    page_json,
    path_id,
    query_int,
    query_offset,
    read_body,
)

routes = web.RouteTableDef()


class BookingVersion(BaseModel):
    """The booking the page showed; a change made elsewhere since then is rejected."""

    model_config = ConfigDict(extra="forbid")

    expected_entry_id: int | None = Field(strict=True)
    expected_order_at: str | None = Field(strict=True)
    # Only for enrollment: the chosen slot of the assignment's group.
    slot_id: int | None = Field(default=None, strict=True)

    def order_at(self) -> datetime | None:
        if self.expected_order_at is None:
            return None
        try:
            return datetime.fromisoformat(self.expected_order_at)
        except ValueError:
            raise InvalidRequest() from None


class SlotDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    start: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    end: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    capacity: int = Field(strict=True)


def hhmm(value: time) -> str:
    return f"{value:%H:%M}"


def slot_json(slot: DefenseSlot, occupied: int, now: datetime) -> dict:
    return {
        "id": slot.id,
        "group_id": slot.group_id,
        "date": slot.date.isoformat(),
        "date_label": f"{slot.date:%d.%m.%Y}",
        "weekday": weekday_label(slot.date),
        "start": hhmm(slot.start_time),
        "end": hhmm(slot.end_time),
        "time_label": interval_label(slot.start_time, slot.end_time),
        "capacity": slot.capacity,
        "occupied": occupied,
        "is_open": slot.is_open,
        "is_past": queue_service.slot_end_at(slot.date, slot.end_time) <= now,
    }


def parse_day(raw: str | None) -> date:
    try:
        return date.fromisoformat(raw or "")
    except ValueError:
        raise InvalidRequest() from None


def group_json(group) -> dict:
    return {"id": group.id, "name": group.name}


@routes.get(r"/api/groups/{id:\d+}/slots")
async def group_slots(request: web.Request) -> web.Response:
    """All slots of the group for its teacher, upcoming ones for its students."""
    session, user, group_id = db(request), current_user(request), path_id(request)
    page = await list_defense_slots(
        session, actor_id=user.id, group_id=group_id, offset=query_offset(request)
    )
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    now = queue_service.now_moscow()
    return web.json_response(
        {
            "group": group_json(group),
            **page_json(page, [slot_json(item.slot, item.occupied, now) for item in page.items]),
        }
    )


@routes.get(r"/api/groups/{id:\d+}/unassigned")
async def unassigned(request: web.Request) -> web.Response:
    page = await list_approved_unassigned(
        db(request),
        actor_id=current_user(request).id,
        group_id=path_id(request),
        offset=query_offset(request),
    )
    return web.json_response(
        page_json(
            page,
            [
                {
                    "name": item.student_name,
                    "assignment_title": item.assignment_title,
                    "attempt_number": item.submission.attempt_number,
                }
                for item in page.items
            ],
        )
    )


@routes.get(r"/api/groups/{id:\d+}/calendar")
async def calendar(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    today = queue_service.today_moscow()
    pages = calendar_pages(today)
    page = query_int(request, "page")
    if page >= len(pages):
        raise InvalidRequest()
    first, last = pages[page]
    # Teacher-only read: it also checks that the group is the teacher's own.
    slots = await list_slots_between(
        session, actor_id=user.id, group_id=group_id, first_day=first, last_day=last
    )
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    marked = {slot.date for slot in slots}
    days = []
    cursor = first
    while cursor <= last:
        days.append(
            {
                "date": cursor.isoformat(),
                "day": cursor.day,
                "month": cursor.month,
                "marked": cursor in marked,
                "today": cursor == today,
            }
        )
        cursor += timedelta(days=1)
    return web.json_response(
        {
            "group": group_json(group),
            "page": page,
            "pages": len(pages),
            "range_label": range_label(first, last),
            "leading": first.weekday(),
            "weekdays": list(WEEKDAYS),
            "days": days,
        }
    )


@routes.get(r"/api/groups/{id:\d+}/day")
async def day_options(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    day = parse_day(request.query.get("date"))
    if day < queue_service.today_moscow():
        raise InvalidRequest("Эта дата уже прошла. Выберите другую.")
    slots = await list_slots_between(
        session, actor_id=user.id, group_id=group_id, first_day=day, last_day=day
    )
    now = queue_service.now_moscow()
    busy = [(slot.start_time, slot.end_time) for slot in slots]
    template = await latest_slot_template(session, actor_id=user.id, group_id=group_id)
    repeat = None
    if template is not None and template_fits(
        day, template.start_time, template.end_time, busy, now
    ):
        repeat = {
            "start": hhmm(template.start_time),
            "end": hhmm(template.end_time),
            "time_label": interval_label(template.start_time, template.end_time),
            "capacity": template.capacity,
        }
    return web.json_response(
        {
            "date": day.isoformat(),
            "date_label": f"{day:%d.%m.%Y}",
            "weekday": weekday_label(day),
            "busy": [
                {
                    "start": hhmm(slot.start_time),
                    "end": hhmm(slot.end_time),
                    "time_label": interval_label(slot.start_time, slot.end_time),
                }
                for slot in slots
            ],
            "starts": [
                {
                    "time": hhmm(from_minutes(start)),
                    "durations": [
                        {
                            "minutes": minutes,
                            "end": hhmm(from_minutes(end)),
                            "label": duration_label(minutes),
                        }
                        for minutes, end in duration_options(day, start, busy, now)
                    ],
                }
                for start in start_options(day, busy, now)
            ],
            "template": repeat,
            "capacities": list(CAPACITIES),
        }
    )


@routes.post(r"/api/groups/{id:\d+}/slots")
async def create_slot(request: web.Request) -> web.Response:
    session, user, group_id = db(request), current_user(request), path_id(request)
    draft = await read_body(request, SlotDraft, error=InvalidDefenseSlot())
    try:
        day = date.fromisoformat(draft.date)
        start, end = time.fromisoformat(draft.start), time.fromisoformat(draft.end)
    except ValueError:
        raise InvalidDefenseSlot() from None

    async def create() -> dict:
        slot = await create_defense_slot(
            session,
            actor_id=user.id,
            group_id=group_id,
            day_date=day,
            start_time=start,
            end_time=end,
            capacity=draft.capacity,
        )
        # After commit, as in the chat: students learn about the new slot.
        await notify_slot_opened(request.app[NOTIFIER], request.app[BOT], session, slot_id=slot.id)
        info = await get_defense_slot(session, actor_id=user.id, slot_id=slot.id)
        return {"slot": slot_json(info.slot, info.occupied, queue_service.now_moscow())}

    body = await request.app[IDEMPOTENCY].run(
        user.id, request.headers.get("Idempotency-Key"), create
    )
    return web.json_response(body, status=201)


@routes.get(r"/api/slots/{id:\d+}/queue")
async def slot_queue(request: web.Request) -> web.Response:
    session, user = db(request), current_user(request)
    queue = await get_slot_queue(
        session, actor_id=user.id, slot_id=path_id(request), offset=query_offset(request)
    )
    group = await get_group(session, actor_id=user.id, group_id=queue.slot.group_id)
    return web.json_response(
        {
            "group": group_json(group),
            "slot": slot_json(queue.slot, queue.occupied, queue_service.now_moscow()),
            **page_json(
                queue.members,
                [
                    {
                        "position": member.position,
                        "name": member.student_name,
                        "username": member.student_username,
                        "assignment_id": member.assignment_id,
                        "assignment_title": member.assignment_title,
                        "attempt_number": member.attempt_number,
                    }
                    for member in queue.members.items
                ],
            ),
        }
    )


async def _slot_response(request: web.Request, slot_id: int, **extra) -> web.Response:
    info = await get_defense_slot(db(request), actor_id=current_user(request).id, slot_id=slot_id)
    return web.json_response(
        {"slot": slot_json(info.slot, info.occupied, queue_service.now_moscow()), **extra}
    )


@routes.post(r"/api/slots/{id:\d+}/close")
async def close_slot(request: web.Request) -> web.Response:
    slot_id = path_id(request)
    await close_defense_slot(db(request), actor_id=current_user(request).id, slot_id=slot_id)
    return await _slot_response(request, slot_id)


@routes.post(r"/api/slots/{id:\d+}/reopen")
async def reopen_slot(request: web.Request) -> web.Response:
    session, slot_id = db(request), path_id(request)
    result = await reopen_defense_slot(session, actor_id=current_user(request).id, slot_id=slot_id)
    # A repeated tap changes nothing and must not notify again.
    if result.reopened:
        await notify_slot_opened(
            request.app[NOTIFIER], request.app[BOT], session, slot_id=slot_id, reopened=True
        )
    return await _slot_response(request, slot_id, reopened=result.reopened)


async def _booking_json(request: web.Request, assignment_id: int) -> dict | None:
    booking = await get_student_booking(
        db(request), actor_id=current_user(request).id, assignment_id=assignment_id
    )
    if booking is None:
        return None
    now = queue_service.now_moscow()
    slot = slot_json(booking.slot, booking.occupied, now)
    return {
        "slot": slot,
        "position": booking.position,
        "occupied": booking.occupied,
        "entry_id": booking.entry.id,
        "order_at": booking.entry.order_at.isoformat(),
        "can_move": booking.slot.is_open and not slot["is_past"],
    }


@routes.get(r"/api/assignments/{id:\d+}/booking")
async def booking(request: web.Request) -> web.Response:
    session, user, assignment_id = db(request), current_user(request), path_id(request)
    submission = await require_approved(session, actor_id=user.id, assignment_id=assignment_id)
    assignment = await get_assignment(session, actor_id=user.id, assignment_id=assignment_id)
    return web.json_response(
        {
            "assignment": assignment_json(assignment),
            "attempt_number": submission.attempt_number,
            "booking": await _booking_json(request, assignment_id),
        }
    )


@routes.get(r"/api/groups/{id:\d+}/defense")
async def defense_works(request: web.Request) -> web.Response:
    """Student: every approved work of the group and where it is booked."""
    session, user, group_id = db(request), current_user(request), path_id(request)
    works = await list_defense_works(session, actor_id=user.id, group_id=group_id)
    group = await get_group(session, actor_id=user.id, group_id=group_id)
    now = queue_service.now_moscow()
    items = []
    for work in works:
        booked = work.booking
        slot = slot_json(booked.slot, booked.occupied, now) if booked else None
        items.append(
            {
                "assignment": assignment_json(work.assignment),
                "attempt_number": work.submission.attempt_number,
                "booking": {"slot": slot, "position": booked.position, "occupied": booked.occupied}
                if booked
                else None,
            }
        )
    return web.json_response({"group": group_json(group), "works": items})


@routes.post(r"/api/assignments/{id:\d+}/enroll")
async def enroll(request: web.Request) -> web.Response:
    # The service owns the transaction, so nothing is read before it; it also checks that
    # the slot belongs to the assignment's group.
    session, user, assignment_id = db(request), current_user(request), path_id(request)
    version = await read_body(request, BookingVersion)
    if version.slot_id is None:
        raise InvalidRequest()
    expected_order = version.order_at()
    entry = await enroll_slot(
        session,
        actor_id=user.id,
        assignment_id=assignment_id,
        slot_id=version.slot_id,
        expected_entry_id=version.expected_entry_id,
        expected_order_at=expected_order,
    )
    # As in the chat: a repeated booking of the current slot changes nothing and is not sent.
    if entry.id != version.expected_entry_id or entry.order_at != expected_order:
        await notify_queue_booking(
            request.app[NOTIFIER],
            request.app[BOT],
            session,
            entry_id=entry.id,
            switched=version.expected_entry_id is not None,
        )
    return web.json_response({"booking": await _booking_json(request, assignment_id)})


@routes.post(r"/api/assignments/{id:\d+}/move-to-end")
async def move_to_tail(request: web.Request) -> web.Response:
    session, user, assignment_id = db(request), current_user(request), path_id(request)
    version = await read_body(request, BookingVersion)
    if version.expected_entry_id is None or version.expected_order_at is None:
        raise InvalidRequest()
    await move_to_end(
        session,
        actor_id=user.id,
        assignment_id=assignment_id,
        expected_entry_id=version.expected_entry_id,
        expected_order_at=version.order_at(),
    )
    return web.json_response({"booking": await _booking_json(request, assignment_id)})


@routes.delete(r"/api/slots/{id:\d+}")
async def cancel_slot(request: web.Request) -> web.Response:
    slot = await cancel_defense_slot(
        db(request), actor_id=current_user(request).id, slot_id=path_id(request)
    )
    return web.json_response({"deleted": True, "group_id": slot.group_id})
