class ModelError(RuntimeError):
    pass


class TransientModelError(ModelError):
    """Retryable provider/network/rate-limit problem."""


class PermanentModelError(ModelError):
    """Non-retryable model configuration or request error."""
