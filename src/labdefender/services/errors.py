import math


class DomainError(Exception):
    """Expected business error with a message safe to show to the user."""


class InvalidDefenseDay(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Укажите дату YYYY-MM-DD не раньше сегодняшней по Москве и положительный лимит мест."
        )


class DefenseDayExists(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "На эту дату у задания уже есть день защиты с другим лимитом. Откройте /queue."
        )


class ApprovalRequired(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Для записи на защиту нужен допуск последней попытки. Проверьте /assignments."
        )


class DefenseDayClosed(DomainError):
    def __init__(self) -> None:
        super().__init__("Эта дата уже прошла. Выберите другой день защиты.")


class DefenseDayFull(DomainError):
    def __init__(self) -> None:
        super().__init__("На выбранный день мест нет. Прежняя запись сохранена, если она была.")


class QueueChanged(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Ваша запись изменилась или недоступна. Откройте очередь заново через /queue."
        )


class InvalidAssignment(DomainError):
    def __init__(self) -> None:
        super().__init__("Нужно название от 1 до 256 символов и условие до 4000 символов.")


class InvalidSolution(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Отправьте один PDF или ZIP до 20 МиБ с известным размером, "
            "текст до 4096 символов или ссылку HTTP(S)."
        )


class SubmissionConflict(DomainError):
    def __init__(self) -> None:
        super().__init__("Попытка уже отправлена, проверена или устарела. Откройте задание заново.")


class InvalidReviewComment(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Для возврата нужен комментарий от 1 до 4000 символов. Введите его заново."
        )


class StaleCourseAction(DomainError):
    def __init__(self) -> None:
        super().__init__("Кнопка устарела. Откройте раздел заново через /help.")


class SolutionDeliveryUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Не удалось показать файл. Попробуйте открыть попытку ещё раз.")


class AccessDenied(DomainError):
    def __init__(self) -> None:
        super().__init__("Недостаточно прав для этого действия.")


class ObjectUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Объект не найден или недоступен.")


class InvalidGroupName(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Название группы должно содержать от 1 до 128 символов. Введите его заново."
        )


class InvalidInvitation(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Приглашение недействительно. Запись в группу не выполнена. Обратитесь к преподавателю."
        )


class GroupCreationUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Не удалось создать группу. Попробуйте ещё раз через /create_group.")


class StaleGroupAction(DomainError):
    def __init__(self) -> None:
        super().__init__("Кнопка устарела. Начните создание заново через /create_group.")


class InvitationUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Не удалось получить ссылку. Попробуйте ещё раз через /invite.")


class UserNotRegistered(DomainError):
    def __init__(self) -> None:
        super().__init__("Пользователь не зарегистрирован. Попросите его сначала выполнить /start.")


class RegistrationDisabled(DomainError):
    def __init__(self) -> None:
        super().__init__("Регистрация по коду отключена. Обратитесь к администратору.")


class InvalidRegistrationCode(DomainError):
    def __init__(self, remaining_attempts: int) -> None:
        self.remaining_attempts = remaining_attempts
        super().__init__(
            f"Неверный код. Осталось попыток: {remaining_attempts}. "
            "Введите код ещё раз или отправьте /cancel."
        )


class RegistrationBlocked(DomainError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        minutes = max(1, math.ceil(retry_after_seconds / 60))
        super().__init__(
            f"Слишком много неверных попыток. Повторите через {minutes} мин. с помощью /teacher."
        )
