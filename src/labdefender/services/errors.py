import math


class DomainError(Exception):
    """Expected business error with a message safe to show to the user."""


class AccessDenied(DomainError):
    def __init__(self) -> None:
        super().__init__("Недостаточно прав для этого действия.")


class ObjectUnavailable(DomainError):
    def __init__(self) -> None:
        super().__init__("Объект не найден или недоступен.")


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
