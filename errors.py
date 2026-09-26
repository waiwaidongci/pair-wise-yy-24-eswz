class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


KEEP = object()
