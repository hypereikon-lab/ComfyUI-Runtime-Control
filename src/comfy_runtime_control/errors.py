"""Typed failures for callers and the CLI."""


class ComfyRuntimeError(RuntimeError):
    """The remote runtime returned an error or an invalid response."""


class GraphValidationError(ValueError):
    """An API graph does not match the captured live node schemas."""


class MutationGuardError(PermissionError):
    """A consequential operation was not explicitly and exactly authorized."""
