"""Exit-coded errors (CLI-03). Messages name the fix."""


class Error(Exception):
    exit = 1


class Usage(Error):
    exit = 2


class NotFound(Error):
    exit = 3


class Refused(Error):
    """Guard refused: state conflict or validation failure."""

    exit = 4


class Stale(Refused):
    """The card changed after the caller read it (WEB-09); the web board answers 409."""


class NotAllowed(Error):
    """Not allowed in this context (worker or owner gating)."""

    exit = 5
