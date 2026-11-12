import asyncio
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update
from sqlalchemy import func, select

from labdefender.app import create_dispatcher
from labdefender.db.models import Membership, StudyGroup
from labdefender.db.session import create_database, initialize_database
from labdefender.enums import Role
from labdefender.keyboards.groups import (
    GroupCreationCallback,
    StudentGroupCallback,
    TeacherGroupCallback,
)
from labdefender.services.errors import (
    AccessDenied,
    GroupCreationUnavailable,
    InvalidGroupName,
    InvalidInvitation,
    ObjectUnavailable,
)
from labdefender.services.groups import (
    GROUP_PAGE_SIZE,
    create_group,
    get_invite_link,
    join_group,
    list_groups,
    parse_invite_payload,
)
from labdefender.services.users import ensure_user


def message_update(text, *, user_id=101):
    message = {
        "message_id": 10,
        "date": datetime.now(UTC),
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": 1, "message": message})


def callback_update(data, *, user_id=101):
    message = message_update("Menu", user_id=user_id).message.model_dump(by_alias=True)
    message["from"] = {"id": 123456, "is_bot": True, "first_name": "Bot"}
    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "group-callback",
                "chat_instance": "test-chat",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": message,
            },
        }
    )


def messages(bot):
    return [call for call in bot.session.calls if isinstance(call, SendMessage)]


@pytest.fixture
async def context(database):
    _, sessions = database
    ids = {}
    async with sessions() as session:
        for telegram_id, name, role in [
            (101, "Teacher", Role.TEACHER),
            (202, "Student", Role.STUDENT),
            (303, "Other teacher", Role.TEACHER),
        ]:
            user = await ensure_user(
                session, telegram_id=telegram_id, username=None, full_name=name
            )
            user.role = role
            await session.commit()
            ids[telegram_id] = user.id
    return sessions, ids


async def make_group(context, *, name="ИВТ-101", teacher=101):
    sessions, ids = context
    async with sessions() as session:
        return await create_group(session, actor_id=ids[teacher], name=name)


async def test_group_invite_join_and_repeat_use_same_persisted_records(context):
    sessions, ids = context
    group = await make_group(context, name="  ИВТ-101\n  Практика  ")
    assert group.name == "ИВТ-101 Практика"
    async with sessions() as session:
        link = await get_invite_link(
            session, actor_id=ids[101], group_id=group.id, bot_username="LabDefenderTestBot"
        )
    payload = parse_qs(urlparse(link).query)["start"][0]
    assert len(payload) <= 64
    assert parse_invite_payload(payload) == group.invite_token
    for joined in (True, False):
        async with sessions() as session:
            result = await join_group(
                session, actor_id=ids[202], invite_token=parse_invite_payload(payload)
            )
            assert result.group.id == group.id
            assert result.joined is joined
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Membership)) == 1
        assert [item.id for item in (await list_groups(session, actor_id=ids[202])).groups] == [
            group.id
        ]
        assert (
            await get_invite_link(
                session, actor_id=ids[101], group_id=group.id, bot_username="LabDefenderTestBot"
            )
            == link
        )


async def test_services_enforce_roles_and_invitation_ownership(context):
    sessions, ids = context
    group = await make_group(context)
    async with sessions() as session:
        with pytest.raises(AccessDenied):
            await create_group(session, actor_id=ids[202], name="Forged")
    async with sessions() as session:
        with pytest.raises(ObjectUnavailable):
            await get_invite_link(
                session, actor_id=ids[303], group_id=group.id, bot_username="LabDefenderTestBot"
            )
    async with sessions() as session:
        with pytest.raises(AccessDenied):
            await get_invite_link(
                session, actor_id=ids[202], group_id=group.id, bot_username="LabDefenderTestBot"
            )
    async with sessions() as session:
        with pytest.raises(AccessDenied):
            await join_group(session, actor_id=ids[101], invite_token=group.invite_token)


@pytest.mark.parametrize("name", ["", " \n\t ", "я" * 129])
async def test_invalid_name_does_not_create_group(context, name):
    with pytest.raises(InvalidGroupName):
        await make_group(context, name=name)
    sessions, _ = context
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 0


async def test_invite_token_collision_is_retried(context, monkeypatch):
    existing = await make_group(context)
    tokens = iter([existing.invite_token, "x" * 32])
    monkeypatch.setattr("labdefender.services.groups.secrets.token_urlsafe", lambda _: next(tokens))
    new_group = await make_group(context, name="Another")
    assert new_group.id != existing.id
    assert new_group.invite_token == "x" * 32


async def test_repeated_token_collisions_leave_no_partial_group(context, monkeypatch):
    existing = await make_group(context)
    monkeypatch.setattr(
        "labdefender.services.groups.secrets.token_urlsafe", lambda _: existing.invite_token
    )
    with pytest.raises(GroupCreationUnavailable):
        await make_group(context, name="Must not persist")
    sessions, _ = context
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1


async def test_parallel_join_adds_membership_once(context):
    sessions, ids = context
    group = await make_group(context)

    async def join():
        async with sessions() as session:
            return await join_group(session, actor_id=ids[202], invite_token=group.invite_token)

    results = await asyncio.gather(*(join() for _ in range(5)))
    assert sum(result.joined for result in results) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Membership)) == 1


@pytest.mark.parametrize(
    "payload", ["join_", "join_short", "join_" + "x" * 33, "join_" + "/" * 32, "unknown"]
)
def test_malformed_invites_are_rejected(payload):
    with pytest.raises(InvalidInvitation):
        parse_invite_payload(payload)


async def test_unknown_invite_does_not_add_membership(context):
    sessions, ids = context
    async with sessions() as session:
        with pytest.raises(InvalidInvitation):
            await join_group(session, actor_id=ids[202], invite_token="x" * 32)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Membership)) == 0


async def test_lists_are_paginated_and_exclude_other_teachers_groups(context):
    sessions, ids = context
    own_groups = [
        await make_group(context, name=f"Group {number}") for number in range(GROUP_PAGE_SIZE + 1)
    ]
    other_group = await make_group(context, name="Private", teacher=303)
    for group in (own_groups[0], other_group):
        async with sessions() as session:
            await join_group(session, actor_id=ids[202], invite_token=group.invite_token)
    async with sessions() as session:
        first = await list_groups(session, actor_id=ids[101])
        second = await list_groups(session, actor_id=ids[101], offset=GROUP_PAGE_SIZE)
        student = await list_groups(session, actor_id=ids[202])
        assert [group.id for group in first.groups] == [group.id for group in own_groups[:-1]]
        assert first.has_next is True
        assert [group.id for group in second.groups] == [own_groups[-1].id]
        assert second.has_next is False
        assert {group.id for group in student.groups} == {own_groups[0].id, other_group.id}


async def test_group_and_membership_survive_database_reopen(context, database):
    sessions, ids = context
    group = await make_group(context)
    async with sessions() as session:
        await join_group(session, actor_id=ids[202], invite_token=group.invite_token)
    old_engine, _ = database
    engine, new_sessions = create_database(str(old_engine.url))
    try:
        await initialize_database(engine)
        async with new_sessions() as session:
            result = await join_group(session, actor_id=ids[202], invite_token=group.invite_token)
            assert result.joined is False
        async with new_sessions() as session:
            assert (await list_groups(session, actor_id=ids[101])).groups[0].id == group.id
    finally:
        await engine.dispose()


async def prepare_creation(dispatcher, bot, name="ИВТ-101"):
    await dispatcher.feed_update(bot, message_update("/create_group"))
    await dispatcher.feed_update(bot, message_update(name))
    return messages(bot)[-1].reply_markup.inline_keyboard[0][0].callback_data


async def test_full_flow_create_invite_and_join_by_new_student(context, bot):
    sessions, ids = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_creation(dispatcher, bot)
    await dispatcher.feed_update(bot, callback_update(confirmation))
    invite = messages(bot)[-1]
    assert "https://t.me/LabDefenderTestBot?start=join_" in invite.text
    payload = invite.text.split("?start=")[1]
    await dispatcher.feed_update(bot, message_update(f"/start {payload}", user_id=404))
    joined, card = messages(bot)[-2:]
    assert joined.text == "Вы присоединились к группе «ИВТ-101»."
    assert joined.reply_markup.keyboard[0][0].text == "📚 Задания"
    assert card.text == "Группа «ИВТ-101»\nВы состоите в этой группе."
    await dispatcher.feed_update(bot, message_update(f"/start {payload}", user_id=404))
    assert "Вы уже в группе" in messages(bot)[-2].text
    await dispatcher.feed_update(bot, message_update("/my_groups", user_id=404))
    open_group = messages(bot)[-1].reply_markup.inline_keyboard[0][0].callback_data
    await dispatcher.feed_update(bot, callback_update(open_group, user_id=404))
    assert "Вы состоите в этой группе" in messages(bot)[-1].text
    async with sessions() as session:
        membership = await session.scalar(select(Membership))
        assert membership.student_id != ids[101]
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1
        assert await session.scalar(select(func.count()).select_from(Membership)) == 1


async def test_repeated_confirmation_does_not_create_duplicate(context, bot):
    sessions, _ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_creation(dispatcher, bot)
    await dispatcher.feed_update(bot, callback_update(confirmation))
    await dispatcher.feed_update(bot, callback_update(confirmation))
    assert isinstance(bot.session.calls[-1], AnswerCallbackQuery)
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1


async def test_old_confirmation_cannot_confirm_new_draft(context, bot):
    sessions, _ = context
    dispatcher = create_dispatcher(sessions)
    old_confirmation = await prepare_creation(dispatcher, bot, "Old draft")
    current_confirmation = await prepare_creation(dispatcher, bot, "Current draft")
    await dispatcher.feed_update(bot, callback_update(old_confirmation))
    assert "устарела" in bot.session.calls[-1].text
    await dispatcher.feed_update(bot, callback_update(current_confirmation))
    async with sessions() as session:
        assert (await session.scalar(select(StudyGroup))).name == "Current draft"


@pytest.mark.parametrize("cancel", ["/cancel", "button", "restart"])
async def test_cancelled_or_lost_draft_does_not_create_group(context, bot, cancel):
    sessions, _ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_creation(dispatcher, bot)
    if cancel == "/cancel":
        await dispatcher.feed_update(bot, message_update(cancel))
    elif cancel == "button":
        button = GroupCreationCallback.unpack(confirmation)
        button.action = "cancel"
        await dispatcher.feed_update(bot, callback_update(button.pack()))
    else:
        dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, callback_update(confirmation))
    assert "устарела" in bot.session.calls[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 0


async def test_delivery_failure_after_commit_does_not_duplicate_group(context, bot, monkeypatch):
    sessions, _ = context
    dispatcher = create_dispatcher(sessions)
    confirmation = await prepare_creation(dispatcher, bot)
    original = bot.session.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        if isinstance(method, SendMessage) and "создана" in method.text:
            raise TelegramNetworkError(method=method, message="Example delivery failure")
        return await original(bot, method, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(bot.session, "make_request", request)
        with pytest.raises(TelegramNetworkError):
            await dispatcher.feed_update(bot, callback_update(confirmation))
    await dispatcher.feed_update(bot, callback_update(confirmation))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(StudyGroup)) == 1
    # Recovery does not depend on the lost success message.
    await dispatcher.feed_update(bot, message_update("/my_groups"))
    assert "ИВТ-101" in messages(bot)[-1].reply_markup.inline_keyboard[0][0].text


async def test_callbacks_do_not_disclose_other_groups_or_invites(context, bot):
    sessions, _ = context
    group = await make_group(context)
    dispatcher = create_dispatcher(sessions)
    for action in ("open", "invite"):
        data = TeacherGroupCallback(action=action, value=group.id).pack()
        await dispatcher.feed_update(bot, callback_update(data, user_id=303))
        assert bot.session.calls[-1].show_alert is True
        assert "недоступен" in bot.session.calls[-1].text
    data = StudentGroupCallback(action="open", value=group.id).pack()
    await dispatcher.feed_update(bot, callback_update(data, user_id=202))
    assert "недоступен" in bot.session.calls[-1].text
    await dispatcher.feed_update(
        bot,
        callback_update(TeacherGroupCallback(action="invite", value=group.id).pack(), user_id=202),
    )
    assert "недоступна" in bot.session.calls[-1].text
    assert not any(group.invite_token in message.text for message in messages(bot))


async def test_invite_selection_and_pagination_are_available_to_owner(context, bot):
    sessions, _ = context
    groups = [await make_group(context, name=f"Group {i}") for i in range(GROUP_PAGE_SIZE + 1)]
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/invite"))
    keyboard = messages(bot)[-1].reply_markup.inline_keyboard
    assert keyboard[-1][0].text == "➕ Создать группу"
    next_page = keyboard[-2][0].callback_data
    await dispatcher.feed_update(bot, callback_update(next_page))
    next_keyboard = messages(bot)[-1].reply_markup.inline_keyboard
    assert f"#{groups[-1].id}" in next_keyboard[0][0].text
    await dispatcher.feed_update(bot, callback_update(next_keyboard[0][0].callback_data))
    assert f"?start=join_{groups[-1].invite_token}" in messages(bot)[-1].text


async def test_student_cannot_create_group_and_teacher_cannot_join_as_student(context, bot):
    sessions, _ = context
    group = await make_group(context)
    dispatcher = create_dispatcher(sessions)
    await dispatcher.feed_update(bot, message_update("/create_group", user_id=202))
    assert "недоступна для вашей роли" in messages(bot)[-1].text
    await dispatcher.feed_update(bot, message_update(f"/start join_{group.invite_token}"))
    assert "Недостаточно прав" in messages(bot)[-1].text
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Membership)) == 0
