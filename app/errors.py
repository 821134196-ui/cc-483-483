class AppError(Exception):
    status = 400

    def __init__(self, message, status=None, extra=None):
        super().__init__(message)
        if status is not None:
            self.status = status
        self.extra = extra or {}


class NotFound(AppError):
    status = 404


class Conflict(AppError):
    status = 409


class PermissionDenied(AppError):
    status = 403


class ValidationError(AppError):
    status = 422


class PreconditionFailed(AppError):
    status = 412
