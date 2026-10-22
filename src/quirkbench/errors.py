"""Action refusals returned to callers without a traceback."""


class RequestRejected(ValueError):
    def __init__(self, message, *, not_started=False):
        super().__init__(message)
        self.not_started = not_started
