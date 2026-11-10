from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import DefenseDay, QueueEntry, Submission, User
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role, SubmissionStatus
from labdefender.services.access import (
    require_assignment_access,
    require_defense_day_access,
    require_user,
)
from labdefender.services.errors import (
    ApprovalRequired,
    DefenseDayClosed,
    DefenseDayExists,
    DefenseDayFull,
    InvalidDefenseDay,
    ObjectUnavailable,
    QueueChanged,
)
from labdefender.services.pagination import PAGE_SIZE, Page, validate_offset
from labdefender.services.submissions import latest_submission

MOSCOW = ZoneInfo("Europe/Moscow")


def today_moscow(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(MOSCOW).date()


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def validate_defense_day(day_date: date, capacity: int) -> None:
    if day_date < today_moscow() or type(capacity) is not int or not 1 <= capacity <= 2**31 - 1:
        raise InvalidDefenseDay()


async def create_defense_day(
    session: AsyncSession, *, actor_id: int, assignment_id: int, day_date: date, capacity: int
) -> DefenseDay:
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
        validate_defense_day(day_date, capacity)
        day = await session.scalar(
            select(DefenseDay).where(
                DefenseDay.assignment_id == assignment_id, DefenseDay.date == day_date
            )
        )
        if day is not None:
            if day.capacity != capacity:
                raise DefenseDayExists()
            return day
        day = DefenseDay(assignment_id=assignment_id, date=day_date, capacity=capacity)
        session.add(day)
        await session.flush()
    return day


async def require_approved(
    session: AsyncSession, *, actor_id: int, assignment_id: int
) -> Submission:
    submission = await latest_submission(session, actor_id=actor_id, assignment_id=assignment_id)
    if (
        submission is None
        or submission.status != SubmissionStatus.APPROVED
        or submission.approved_at is None
    ):
        raise ApprovalRequired()
    return submission


@dataclass(frozen=True)
class DayItem:
    day: DefenseDay
    occupied: int


async def list_defense_days(
    session: AsyncSession, *, actor_id: int, assignment_id: int, offset: int = 0
) -> Page[DayItem]:
    validate_offset(offset)
    user = await require_user(session, actor_id=actor_id)
    await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
    query = (
        select(DefenseDay, func.count(QueueEntry.id))
        .outerjoin(QueueEntry, QueueEntry.defense_day_id == DefenseDay.id)
        .where(DefenseDay.assignment_id == assignment_id)
    )
    if user.role == Role.STUDENT:
        await require_approved(session, actor_id=actor_id, assignment_id=assignment_id)
        query = query.where(DefenseDay.date >= today_moscow())
    rows = (
        await session.execute(
            query.group_by(DefenseDay.id)
            .order_by(DefenseDay.date, DefenseDay.id)
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    return Page(
        items=tuple(DayItem(*row) for row in rows[:PAGE_SIZE]),
        offset=offset,
        has_next=len(rows) > PAGE_SIZE,
    )


async def get_defense_day(session: AsyncSession, *, actor_id: int, defense_day_id: int) -> DayItem:
    day = await require_defense_day_access(
        session, actor_id=actor_id, defense_day_id=defense_day_id
    )
    occupied = await session.scalar(
        select(func.count()).select_from(QueueEntry).where(QueueEntry.defense_day_id == day.id)
    )
    return DayItem(day=day, occupied=occupied)


async def _entry(session: AsyncSession, *, actor_id: int, assignment_id: int) -> QueueEntry | None:
    return (
        await session.scalars(
            select(QueueEntry)
            .join(Submission, Submission.id == QueueEntry.submission_id)
            .where(Submission.student_id == actor_id, Submission.assignment_id == assignment_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()


def _check_version(
    entry: QueueEntry | None, expected_entry_id: int | None, expected_order_at: datetime | None
) -> None:
    if entry is None:
        if expected_entry_id is not None or expected_order_at is not None:
            raise QueueChanged()
    elif entry.id != expected_entry_id or entry.order_at != expected_order_at:
        raise QueueChanged()


async def _after_tail(session: AsyncSession, *, defense_day_id: int) -> datetime:
    tail = await session.scalar(
        select(func.max(QueueEntry.order_at)).where(QueueEntry.defense_day_id == defense_day_id)
    )
    now = utc_now()
    return max(now, tail + timedelta(microseconds=1)) if tail is not None else now


async def enroll_day(
    session: AsyncSession,
    *,
    actor_id: int,
    assignment_id: int,
    defense_day_id: int,
    expected_entry_id: int | None,
    expected_order_at: datetime | None,
) -> QueueEntry:
    async with immediate_transaction(session):
        submission = await require_approved(session, actor_id=actor_id, assignment_id=assignment_id)
        day = await require_defense_day_access(
            session, actor_id=actor_id, defense_day_id=defense_day_id
        )
        if day.assignment_id != assignment_id:
            raise ObjectUnavailable()
        if day.date < today_moscow():
            raise DefenseDayClosed()
        entry = await _entry(session, actor_id=actor_id, assignment_id=assignment_id)
        # Repeated enrollment into the current day is a read-only success, preserving the order.
        if (
            entry is not None
            and entry.submission_id == submission.id
            and entry.defense_day_id == day.id
        ):
            return entry
        _check_version(entry, expected_entry_id, expected_order_at)
        occupied = await session.scalar(
            select(func.count()).select_from(QueueEntry).where(QueueEntry.defense_day_id == day.id)
        )
        if occupied >= day.capacity:
            raise DefenseDayFull()
        if entry is None:
            entry = QueueEntry(
                defense_day_id=day.id, submission_id=submission.id, order_at=submission.approved_at
            )
            session.add(entry)
        else:
            if entry.submission_id != submission.id:
                raise QueueChanged()
            new_order = await _after_tail(session, defense_day_id=day.id)
            entry.defense_day_id = day.id
            entry.order_at = new_order
        await session.flush()
    return entry


async def move_to_end(
    session: AsyncSession,
    *,
    actor_id: int,
    assignment_id: int,
    expected_entry_id: int,
    expected_order_at: datetime,
) -> QueueEntry:
    async with immediate_transaction(session):
        submission = await require_approved(session, actor_id=actor_id, assignment_id=assignment_id)
        entry = await _entry(session, actor_id=actor_id, assignment_id=assignment_id)
        _check_version(entry, expected_entry_id, expected_order_at)
        if entry is None or entry.submission_id != submission.id:
            raise QueueChanged()
        day = await session.get(DefenseDay, entry.defense_day_id)
        if day.date < today_moscow():
            raise DefenseDayClosed()
        tail_id = await session.scalar(
            select(QueueEntry.id)
            .where(QueueEntry.defense_day_id == day.id)
            .order_by(QueueEntry.order_at.desc(), QueueEntry.id.desc())
            .limit(1)
        )
        if tail_id != entry.id:
            entry.order_at = await _after_tail(session, defense_day_id=day.id)
    return entry


def _ranked(defense_day_id: int):
    return (
        select(
            QueueEntry.id.label("entry_id"),
            Submission.id.label("submission_id"),
            User.full_name.label("student_name"),
            func.row_number().over(order_by=(QueueEntry.order_at, QueueEntry.id)).label("position"),
            func.count().over().label("total"),
        )
        .select_from(QueueEntry)
        .join(Submission, Submission.id == QueueEntry.submission_id)
        .join(User, User.id == Submission.student_id)
        .where(QueueEntry.defense_day_id == defense_day_id)
        .subquery()
    )


@dataclass(frozen=True)
class StudentBooking:
    entry: QueueEntry
    day: DefenseDay
    position: int
    occupied: int


async def get_student_booking(
    session: AsyncSession, *, actor_id: int, assignment_id: int
) -> StudentBooking | None:
    await require_user(session, actor_id=actor_id, role=Role.STUDENT)
    await require_assignment_access(session, actor_id=actor_id, assignment_id=assignment_id)
    entry = await _entry(session, actor_id=actor_id, assignment_id=assignment_id)
    if entry is None:
        return None
    day = await session.get(DefenseDay, entry.defense_day_id)
    ranked = _ranked(day.id)
    row = (
        await session.execute(
            select(ranked.c.position, ranked.c.total).where(ranked.c.entry_id == entry.id)
        )
    ).one_or_none()
    if row is None:
        raise QueueChanged()
    return StudentBooking(entry=entry, day=day, position=row.position, occupied=row.total)


@dataclass(frozen=True)
class QueueMember:
    entry_id: int
    submission_id: int
    student_name: str
    position: int


@dataclass(frozen=True)
class DayQueue:
    day: DefenseDay
    occupied: int
    members: Page[QueueMember]


async def get_day_queue(
    session: AsyncSession, *, actor_id: int, defense_day_id: int, offset: int = 0
) -> DayQueue:
    validate_offset(offset)
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    info = await get_defense_day(session, actor_id=actor_id, defense_day_id=defense_day_id)
    ranked = _ranked(defense_day_id)
    rows = (
        await session.execute(
            select(ranked).order_by(ranked.c.position).offset(offset).limit(PAGE_SIZE + 1)
        )
    ).all()
    occupied = rows[0].total if rows else info.occupied
    return DayQueue(
        day=info.day,
        occupied=occupied,
        members=Page(
            items=tuple(
                QueueMember(row.entry_id, row.submission_id, row.student_name, row.position)
                for row in rows[:PAGE_SIZE]
            ),
            offset=offset,
            has_next=len(rows) > PAGE_SIZE,
        ),
    )
