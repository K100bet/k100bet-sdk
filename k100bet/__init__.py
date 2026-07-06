"""K100bet Agent SDK — prediction market client for AI agents."""

from .client import (
    HOUSE_FEE_RATE,
    K100bet,
    K100betAuthError,
    K100betError,
    K100betNotFoundError,
    K100betRateLimitError,
    K100betServerError,
    K100betValidationError,
    main,
)

__version__ = "0.1.0"

__all__ = [
    "HOUSE_FEE_RATE",
    "K100bet",
    "K100betError",
    "K100betAuthError",
    "K100betRateLimitError",
    "K100betValidationError",
    "K100betNotFoundError",
    "K100betServerError",
    "main",
    "__version__",
]
