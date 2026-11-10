from enum import StrEnum


class Role(StrEnum):
    STUDENT = "student"
    TEACHER = "teacher"


class SubmissionKind(StrEnum):
    PDF = "pdf"
    ZIP = "zip"
    TEXT = "text"
    LINK = "link"


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    RETURNED = "returned"
    APPROVED = "approved"
