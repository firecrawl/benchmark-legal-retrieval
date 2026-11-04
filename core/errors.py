from pydantic import ValidationError


class EvalError(Exception):
    """User-facing error: bad config, missing file, unknown component."""


def first_error(error: ValidationError) -> str:
    """The first failing field of a validation error as one line: `location: message`."""
    detail = error.errors()[0]
    location = ".".join(str(piece) for piece in detail["loc"])
    return f"{location}: {detail['msg']}" if location else detail["msg"]
