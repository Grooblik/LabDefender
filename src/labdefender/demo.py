import asyncio
from datetime import time, timedelta
from pathlib import Path

from sqlalchemy.engine import URL

from labdefender.db.session import create_database, initialize_database
from labdefender.enums import AssignmentFormat, Role, SubmissionStatus
from labdefender.local import LocalOperationError, check_database, database_lock, reserve_database
from labdefender.services.assignments import create_assignment
from labdefender.services.groups import create_group, join_group
from labdefender.services.queue import create_defense_slot, enroll_slot, today_moscow
from labdefender.services.submissions import review_submission, submit_solution, text_solution
from labdefender.services.users import ensure_user

DEFAULT_TEACHER_ID = 900000001
DEFAULT_STUDENT_IDS = (900000002, 900000003)


async def seed_demo(path: Path, *, teacher_id: int, student_ids: tuple[int, ...]) -> dict:
    all_ids = (teacher_id, *student_ids)
    if (
        not student_ids
        or len(set(all_ids)) != len(all_ids)
        or any(not 0 < value < 2**63 for value in all_ids)
    ):
        raise LocalOperationError(
            "Нужны разные положительные Telegram ID преподавателя и студентов."
        )
    path = await asyncio.to_thread(path.resolve)
    with database_lock(path):
        reserve_database(path)
        engine, sessions = create_database(str(URL.create("sqlite+aiosqlite", database=str(path))))
        completed = False
        try:
            await initialize_database(engine)

            async def call(service, **kwargs):
                async with sessions() as session:
                    return await service(session, **kwargs)

            teacher = await call(
                ensure_user, telegram_id=teacher_id, username=None, full_name="Демо преподаватель"
            )
            async with sessions() as session:
                session.add(teacher)
                teacher.role = Role.TEACHER
                await session.commit()
            group = await call(create_group, actor_id=teacher.id, name="Демо · ИВТ-101")
            assignments = []
            assignment_formats = (
                ("ЛР 1 · готовая очередь", AssignmentFormat.IN_PERSON),
                ("ЛР 2 · на проверке", AssignmentFormat.ONLINE),
                ("ЛР 3 · пересдача", AssignmentFormat.ONLINE),
            )
            for title, assignment_format in assignment_formats:
                assignments.append(
                    await call(
                        create_assignment,
                        actor_id=teacher.id,
                        group_id=group.id,
                        title=title,
                        description="Реализуйте решение, объясните алгоритм и оцените сложность.",
                        format=assignment_format,
                    )
                )
            first_slot = await call(
                create_defense_slot,
                actor_id=teacher.id,
                assignment_id=assignments[0].id,
                day_date=today_moscow() + timedelta(days=1),
                start_time=time(14, 0),
                end_time=time(16, 0),
                capacity=max(2, len(student_ids)),
            )
            await call(
                create_defense_slot,
                actor_id=teacher.id,
                assignment_id=assignments[0].id,
                day_date=today_moscow() + timedelta(days=1),
                start_time=time(16, 0),
                end_time=time(18, 0),
                capacity=1,
            )
            await call(
                create_defense_slot,
                actor_id=teacher.id,
                assignment_id=assignments[0].id,
                day_date=today_moscow() + timedelta(days=2),
                start_time=time(10, 0),
                end_time=time(12, 0),
                capacity=1,
            )
            for index, telegram_id in enumerate(student_ids):
                student = await call(
                    ensure_user,
                    telegram_id=telegram_id,
                    username=None,
                    full_name=f"Демо студент {index + 1}",
                )
                await call(join_group, actor_id=student.id, invite_token=group.invite_token)
                submission = await call(
                    submit_solution,
                    actor_id=student.id,
                    assignment_id=assignments[0].id,
                    expected_previous_id=None,
                    solution=text_solution("Решение для защиты: O(n), проверены крайние случаи."),
                )
                await call(
                    review_submission,
                    actor_id=teacher.id,
                    submission_id=submission.id,
                    decision=SubmissionStatus.APPROVED,
                )
                await call(
                    enroll_slot,
                    actor_id=student.id,
                    assignment_id=assignments[0].id,
                    slot_id=first_slot.id,
                    expected_entry_id=None,
                    expected_order_at=None,
                )
                if index == 0:
                    await call(
                        submit_solution,
                        actor_id=student.id,
                        assignment_id=assignments[1].id,
                        expected_previous_id=None,
                        solution=text_solution("https://example.com/demo-solution"),
                    )
                    returned = await call(
                        submit_solution,
                        actor_id=student.id,
                        assignment_id=assignments[2].id,
                        expected_previous_id=None,
                        solution=text_solution("Первая версия решения"),
                    )
                    await call(
                        review_submission,
                        actor_id=teacher.id,
                        submission_id=returned.id,
                        decision=SubmissionStatus.RETURNED,
                        comment="Добавьте обработку пустого ввода и поясните сложность.",
                    )
            completed = True
        finally:
            await engine.dispose()
            if not completed:
                path.unlink(missing_ok=True)
        check_database(path)
        return {
            "database": str(path),
            "teacher_id": teacher_id,
            "student_ids": list(student_ids),
            "group_id": group.id,
            "assignment_ids": [item.id for item in assignments],
            "slot_ids": [first_slot.id],
            "invite_payload": f"join_{group.invite_token}",
        }
