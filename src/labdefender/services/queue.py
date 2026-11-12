"""Defense queue of a group (discipline): slots with places, each place is one approved work.

A queue entry names the student, the assignment and the approved attempt; a student may book
several works, one entry per assignment. Order inside a slot: `(order_at, id)`; the first
booking takes the approval time, moving to the end or to another slot goes after the tail.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from labdefender.db.models import (
    Assignment,
    DefenseSlot,
    QueueEntry,
    StudyGroup,
    Submission,
    User,
)
from labdefender.db.transactions import immediate_transaction
from labdefender.enums import Role, SubmissionStatus
from labdefender.services.access import (
    require_assignment_access,
    require_defense_slot_access,
    require_group_member,
    require_group_owner,
    require_user,
)
from labdefender.services.errors import (
    ApprovalRequired,
    DefenseSlotClosed,
    DefenseSlotExists,
    DefenseSlotFull,
    DefenseSlotNotEmpty,
    DefenseSlotOverlap,
    InvalidDefenseSlot,
    ObjectUnavailable,
    QueueChanged,
)
from labdefender.services.pagination import PAGE_SIZE, Page, validate_offset
from labdefender.services.submissions import latest_submission

MOSCOW = ZoneInfo("Europe/Moscow")


def now_moscow() -> datetime:
    return datetime.now(MOSCOW)


def today_moscow(now: datetime | None = None) -> date:
    return (now or now_moscow()).astimezone(MOSCOW).date()


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def slot_end_at(day_date: date, end_time: time) -> datetime:
    return datetime.combine(day_date, end_time, tzinfo=MOSCOW)


def validate_defense_slot(day_date: date, start_time: time, end_time: time, capacity: int) -> None:
    if (
        not isinstance(start_time, time)
        or not isinstance(end_time, time)
        or start_time >= end_time
        or type(capacity) is not int
        or not 1 <= capacity <= 2**31 - 1
        or day_date < today_moscow()
        or slot_end_at(day_date, end_time) <= now_moscow()
    ):
        raise InvalidDefenseSlot()


async def create_defense_slot(
    session: AsyncSession,
    *,
    actor_id: int,
    group_id: int,
    day_date: date,
    start_time: time,
    end_time: time,
    capacity: int,
) -> DefenseSlot:
    async with immediate_transaction(session):
        await require_group_owner(session, actor_id=actor_id, group_id=group_id)
        validate_defense_slot(day_date, start_time, end_time, capacity)
        existing = await session.scalar(
            select(DefenseSlot)
            .where(
                DefenseSlot.group_id == group_id,
                DefenseSlot.date == day_date,
                DefenseSlot.start_time == start_time,
            )
            .order_by(DefenseSlot.id)
            .limit(1)
        )
        if existing is not None:
            if existing.end_time == end_time and existing.capacity == capacity:
                return existing
            raise DefenseSlotExists()
        overlap = await session.scalar(
            select(DefenseSlot.id).where(
                DefenseSlot.group_id == group_id,
                DefenseSlot.date == day_date,
                DefenseSlot.start_time < end_time,
                DefenseSlot.end_time > start_time,
            )
        )
        if overlap is not None:
            raise DefenseSlotOverlap()
        slot = DefenseSlot(
            group_id=group_id,
            date=day_date,
            start_time=start_time,
            end_time=end_time,
            capacity=capacity,
        )
        session.add(slot)
        await session.flush()
    return slot


async def close_defense_slot(session: AsyncSession, *, actor_id: int, slot_id: int) -> DefenseSlot:
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        slot = await require_defense_slot_access(session, actor_id=actor_id, slot_id=slot_id)
        slot.is_open = False
    return slot


@dataclass(frozen=True)
class ReopenSlotResult:
    slot: DefenseSlot
    reopened: bool  # False when the slot was already open: nothing changed.


async def reopen_defense_slot(
    session: AsyncSession, *, actor_id: int, slot_id: int
) -> ReopenSlotResult:
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        slot = await require_defense_slot_access(session, actor_id=actor_id, slot_id=slot_id)
        if slot_end_at(slot.date, slot.end_time) <= now_moscow():
            raise DefenseSlotClosed()
        reopened = not slot.is_open
        slot.is_open = True
    return ReopenSlotResult(slot=slot, reopened=reopened)


async def cancel_defense_slot(session: AsyncSession, *, actor_id: int, slot_id: int) -> DefenseSlot:
    async with immediate_transaction(session):
        await require_user(session, actor_id=actor_id, role=Role.TEACHER)
        slot = await require_defense_slot_access(session, actor_id=actor_id, slot_id=slot_id)
        occupied = await session.scalar(
            select(func.count()).select_from(QueueEntry).where(QueueEntry.slot_id == slot.id)
        )
        if occupied:
            raise DefenseSlotNotEmpty()
        await session.delete(slot)
    return slot


async def list_slots_between(
    session: AsyncSession, *, actor_id: int, group_id: int, first_day: date, last_day: date
) -> tuple[DefenseSlot, ...]:
    """Teacher-only read of a group's slots in a date range, for the slot wizard."""
    await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    slots = await session.scalars(
        select(DefenseSlot)
        .where(
            DefenseSlot.group_id == group_id,
            DefenseSlot.date >= first_day,
            DefenseSlot.date <= last_day,
        )
        .order_by(DefenseSlot.date, DefenseSlot.start_time, DefenseSlot.id)
    )
    return tuple(slots)


async def latest_slot_template(
    session: AsyncSession, *, actor_id: int, group_id: int
) -> DefenseSlot | None:
    """The teacher's most relevant previous slot: same group first, then any own group."""
    await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    return await session.scalar(
        select(DefenseSlot)
        .join(StudyGroup, StudyGroup.id == DefenseSlot.group_id)
        .where(StudyGroup.teacher_id == actor_id)
        .order_by((DefenseSlot.group_id == group_id).desc(), DefenseSlot.id.desc())
        .limit(1)
    )


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
class SlotItem:
    slot: DefenseSlot
    occupied: int


async def list_defense_slots(
    session: AsyncSession, *, actor_id: int, group_id: int, offset: int = 0
) -> Page[SlotItem]:
    """The group's slots: all of them for its teacher, upcoming ones for its students."""
    validate_offset(offset)
    user = await require_user(session, actor_id=actor_id)
    if user.role == Role.TEACHER:
        await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    else:
        await require_group_member(session, actor_id=actor_id, group_id=group_id)
    query = (
        select(DefenseSlot, func.count(QueueEntry.id))
        .outerjoin(QueueEntry, QueueEntry.slot_id == DefenseSlot.id)
        .where(DefenseSlot.group_id == group_id)
    )
    if user.role == Role.STUDENT:
        now = now_moscow()
        query = query.where(
            or_(
                DefenseSlot.date > now.date(),
                and_(DefenseSlot.date == now.date(), DefenseSlot.end_time > now.time()),
            )
        )
    rows = (
        await session.execute(
            query.group_by(DefenseSlot.id)
            .order_by(DefenseSlot.date, DefenseSlot.start_time, DefenseSlot.id)
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    return Page(
        items=tuple(SlotItem(*row) for row in rows[:PAGE_SIZE]),
        offset=offset,
        has_next=len(rows) > PAGE_SIZE,
    )


async def get_defense_slot(session: AsyncSession, *, actor_id: int, slot_id: int) -> SlotItem:
    slot = await require_defense_slot_access(session, actor_id=actor_id, slot_id=slot_id)
    occupied = await session.scalar(
        select(func.count()).select_from(QueueEntry).where(QueueEntry.slot_id == slot.id)
    )
    return SlotItem(slot=slot, occupied=occupied)


async def _entry(session: AsyncSession, *, actor_id: int, assignment_id: int) -> QueueEntry | None:
    return (
        await session.scalars(
            select(QueueEntry)
            .where(QueueEntry.student_id == actor_id, QueueEntry.assignment_id == assignment_id)
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


async def _after_tail(session: AsyncSession, *, slot_id: int) -> datetime:
    tail = await session.scalar(
        select(func.max(QueueEntry.order_at)).where(QueueEntry.slot_id == slot_id)
    )
    now = utc_now()
    return max(now, tail + timedelta(microseconds=1)) if tail is not None else now


async def enroll_slot(
    session: AsyncSession,
    *,
    actor_id: int,
    assignment_id: int,
    slot_id: int,
    expected_entry_id: int | None,
    expected_order_at: datetime | None,
) -> QueueEntry:
    async with immediate_transaction(session):
        submission = await require_approved(session, actor_id=actor_id, assignment_id=assignment_id)
        slot = await require_defense_slot_access(session, actor_id=actor_id, slot_id=slot_id)
        assignment = await session.get(Assignment, assignment_id)
        # Any approved work of the group may take a place in any of the group's slots.
        if assignment is None or slot.group_id != assignment.group_id:
            raise ObjectUnavailable()
        if not slot.is_open or slot_end_at(slot.date, slot.end_time) <= now_moscow():
            raise DefenseSlotClosed()
        entry = await _entry(session, actor_id=actor_id, assignment_id=assignment_id)
        # Repeated enrollment into the current slot is a read-only success, preserving the order.
        if entry is not None and entry.submission_id == submission.id and entry.slot_id == slot.id:
            return entry
        _check_version(entry, expected_entry_id, expected_order_at)
        occupied = await session.scalar(
            select(func.count()).select_from(QueueEntry).where(QueueEntry.slot_id == slot.id)
        )
        if occupied >= slot.capacity:
            raise DefenseSlotFull()
        if entry is None:
            entry = QueueEntry(
                slot_id=slot.id,
                submission_id=submission.id,
                student_id=actor_id,
                assignment_id=assignment_id,
                order_at=submission.approved_at,
            )
            session.add(entry)
        else:
            if entry.submission_id != submission.id:
                raise QueueChanged()
            entry.slot_id = slot.id
            entry.order_at = await _after_tail(session, slot_id=slot.id)
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
        slot = await session.get(DefenseSlot, entry.slot_id)
        if (
            slot is None
            or not slot.is_open
            or slot_end_at(slot.date, slot.end_time) <= now_moscow()
        ):
            raise DefenseSlotClosed()
        tail_id = await session.scalar(
            select(QueueEntry.id)
            .where(QueueEntry.slot_id == slot.id)
            .order_by(QueueEntry.order_at.desc(), QueueEntry.id.desc())
            .limit(1)
        )
        if tail_id != entry.id:
            entry.order_at = await _after_tail(session, slot_id=slot.id)
    return entry


def _ranked(slot_id: int):
    return (
        select(
            QueueEntry.id.label("entry_id"),
            Submission.id.label("submission_id"),
            User.full_name.label("student_name"),
            User.username.label("student_username"),
            Assignment.id.label("assignment_id"),
            Assignment.title.label("assignment_title"),
            Submission.attempt_number.label("attempt_number"),
            func.row_number().over(order_by=(QueueEntry.order_at, QueueEntry.id)).label("position"),
            func.count().over().label("total"),
        )
        .select_from(QueueEntry)
        .join(Submission, Submission.id == QueueEntry.submission_id)
        .join(User, User.id == QueueEntry.student_id)
        .join(Assignment, Assignment.id == QueueEntry.assignment_id)
        .where(QueueEntry.slot_id == slot_id)
        .subquery()
    )


@dataclass(frozen=True)
class StudentBooking:
    entry: QueueEntry
    slot: DefenseSlot
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
    slot = await session.get(DefenseSlot, entry.slot_id)
    if slot is None:
        raise QueueChanged()
    ranked = _ranked(slot.id)
    row = (
        await session.execute(
            select(ranked.c.position, ranked.c.total).where(ranked.c.entry_id == entry.id)
        )
    ).one_or_none()
    if row is None:
        raise QueueChanged()
    return StudentBooking(entry=entry, slot=slot, position=row.position, occupied=row.total)


@dataclass(frozen=True)
class QueueMember:
    """A place in a slot: who defends which work (assignment and attempt)."""

    entry_id: int
    submission_id: int
    student_name: str
    position: int
    student_username: str | None = None
    assignment_id: int | None = None
    assignment_title: str = ""
    attempt_number: int = 0


@dataclass(frozen=True)
class SlotQueue:
    slot: DefenseSlot
    occupied: int
    members: Page[QueueMember]


async def get_slot_queue(
    session: AsyncSession, *, actor_id: int, slot_id: int, offset: int = 0
) -> SlotQueue:
    validate_offset(offset)
    await require_user(session, actor_id=actor_id, role=Role.TEACHER)
    info = await get_defense_slot(session, actor_id=actor_id, slot_id=slot_id)
    ranked = _ranked(slot_id)
    rows = (
        await session.execute(
            select(ranked).order_by(ranked.c.position).offset(offset).limit(PAGE_SIZE + 1)
        )
    ).all()
    occupied = rows[0].total if rows else info.occupied
    return SlotQueue(
        slot=info.slot,
        occupied=occupied,
        members=Page(
            items=tuple(
                QueueMember(
                    entry_id=row.entry_id,
                    submission_id=row.submission_id,
                    student_name=row.student_name,
                    position=row.position,
                    student_username=row.student_username,
                    assignment_id=row.assignment_id,
                    assignment_title=row.assignment_title,
                    attempt_number=row.attempt_number,
                )
                for row in rows[:PAGE_SIZE]
            ),
            offset=offset,
            has_next=len(rows) > PAGE_SIZE,
        ),
    )


def approved_unassigned_query(group_id: int, *columns):
    """Approved works of the group (latest attempt of a student per assignment) not yet booked."""
    latest_attempts = (
        select(
            Submission.student_id,
            Submission.assignment_id,
            func.max(Submission.attempt_number).label("max_attempt"),
        )
        .join(Assignment, Assignment.id == Submission.assignment_id)
        .where(Assignment.group_id == group_id)
        .group_by(Submission.student_id, Submission.assignment_id)
        .subquery()
    )
    return (
        select(*columns)
        .select_from(Submission)
        .join(
            latest_attempts,
            and_(
                Submission.student_id == latest_attempts.c.student_id,
                Submission.assignment_id == latest_attempts.c.assignment_id,
                Submission.attempt_number == latest_attempts.c.max_attempt,
            ),
        )
        .join(User, User.id == Submission.student_id)
        .join(Assignment, Assignment.id == Submission.assignment_id)
        .outerjoin(QueueEntry, QueueEntry.submission_id == Submission.id)
        .where(
            Submission.status == SubmissionStatus.APPROVED,
            QueueEntry.id.is_(None),
        )
    )


@dataclass(frozen=True)
class UnbookedWork:
    submission: Submission
    student_name: str
    assignment_title: str


async def list_approved_unassigned(
    session: AsyncSession, *, actor_id: int, group_id: int, offset: int = 0
) -> Page[UnbookedWork]:
    """Teacher: approved works of the group that are not booked into any slot yet."""
    validate_offset(offset)
    await require_group_owner(session, actor_id=actor_id, group_id=group_id)
    query = approved_unassigned_query(group_id, Submission, User.full_name, Assignment.title)
    rows = (
        await session.execute(
            query.order_by(Submission.approved_at, Submission.id)
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        )
    ).all()
    return Page(
        items=tuple(UnbookedWork(*row) for row in rows[:PAGE_SIZE]),
        offset=offset,
        has_next=len(rows) > PAGE_SIZE,
    )


@dataclass(frozen=True)
class DefenseWork:
    """A student's approved work in a group and its place in the queue, if booked."""

    assignment: Assignment
    submission: Submission
    booking: "StudentBooking | None"


async def list_defense_works(
    session: AsyncSession, *, actor_id: int, group_id: int
) -> tuple[DefenseWork, ...]:
    """Student: every approved work of the group, booked or not, in assignment order."""
    await require_user(session, actor_id=actor_id, role=Role.STUDENT)
    await require_group_member(session, actor_id=actor_id, group_id=group_id)
    assignments = (
        await session.scalars(
            select(Assignment).where(Assignment.group_id == group_id).order_by(Assignment.id)
        )
    ).all()
    works = []
    for assignment in assignments:
        submission = await latest_submission(
            session, actor_id=actor_id, assignment_id=assignment.id
        )
        if submission is None or submission.status != SubmissionStatus.APPROVED:
            continue
        booking = await get_student_booking(session, actor_id=actor_id, assignment_id=assignment.id)
        works.append(DefenseWork(assignment=assignment, submission=submission, booking=booking))
    return tuple(works)
