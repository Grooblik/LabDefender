"""JSON views of records shared by several API modules."""

from datetime import UTC, datetime

from labdefender.db.models import Submission
from labdefender.handlers.notifications import KIND_LABELS
from labdefender.services.queue import MOSCOW
from labdefender.services.submissions import STATUS_LABELS


def moscow_label(moment: datetime | None) -> str | None:
    # Timestamps are stored as naive UTC.
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


def submission_json(submission: Submission, *, student_name: str | None = None) -> dict:
    return {
        "id": submission.id,
        "assignment_id": submission.assignment_id,
        "attempt_number": submission.attempt_number,
        "status": submission.status.value,
        "status_label": STATUS_LABELS[submission.status],
        "kind": submission.kind.value,
        "kind_label": KIND_LABELS[submission.kind],
        "has_file": submission.file_id is not None,
        "submitted_label": moscow_label(submission.submitted_at),
        "comment": submission.review_comment,
        "student_name": student_name,
    }
