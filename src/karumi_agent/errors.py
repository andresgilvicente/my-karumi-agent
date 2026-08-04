"""Typed errors.

Every failure the agent can produce is one of these, carrying enough structured
context to be logged as JSON and surfaced to the operator without string parsing.
"""

from __future__ import annotations

from typing import Any


class KarumiError(Exception):
    """Base class for every error this package raises."""

    #: Stable, machine-readable code. Used in JSON logs and action results.
    code: str = "karumi_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def as_dict(self) -> dict[str, Any]:
        """Render as a JSON-serialisable payload."""
        return {"code": self.code, "message": self.message, **self.context}

    def __str__(self) -> str:
        if not self.context:
            return self.message
        detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({detail})"


# --------------------------------------------------------------------------
# Configuration / environment
# --------------------------------------------------------------------------
class ConfigError(KarumiError):
    """Configuration is missing, malformed, or violates a hard constraint."""

    code = "config_error"


class EditorialConfigError(ConfigError):
    """Editorial config contains something it is not allowed to contain.

    The brief forbids selectors, element ids, coordinates, hardcoded routes and
    deterministic click scripts in product configuration. This is raised when the
    loader detects one, so the constraint is enforced rather than merely promised.
    """

    code = "editorial_config_error"


class DependencyError(KarumiError):
    """A required local dependency is missing or unusable.

    Carries a remediation hint so ``karumi doctor`` can tell the operator exactly
    what to run.
    """

    code = "dependency_error"

    def __init__(self, message: str, *, remedy: str | None = None, **context: Any) -> None:
        super().__init__(message, **context)
        self.remedy = remedy

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        if self.remedy:
            payload["remedy"] = self.remedy
        return payload


# --------------------------------------------------------------------------
# Browser observation and control
# --------------------------------------------------------------------------
class BrowserError(KarumiError):
    """Something went wrong driving the browser."""

    code = "browser_error"


class SnapshotError(BrowserError):
    """A page snapshot could not be captured or parsed."""

    code = "snapshot_error"


class StaleSnapshotError(BrowserError):
    """An action referenced a snapshot that is no longer current.

    Refs are only meaningful against the snapshot they came from. Acting on a
    stale one risks clicking whatever moved into that position, so we refuse and
    force a re-observation instead.
    """

    code = "stale_snapshot"

    def __init__(self, *, requested: str, current: str | None) -> None:
        super().__init__(
            "Snapshot is stale; re-observe the page before acting",
            requested_snapshot=requested,
            current_snapshot=current,
        )


class RefNotFoundError(BrowserError):
    """A ref did not resolve to a live element in the current page."""

    code = "ref_not_found"

    def __init__(self, ref: str, *, snapshot_id: str | None = None) -> None:
        super().__init__(
            f"Element ref {ref!r} is not present on the page", ref=ref, snapshot_id=snapshot_id
        )
        self.ref = ref


class ActionFailedError(BrowserError):
    """An action was attempted but did not complete."""

    code = "action_failed"


class ActionTimeoutError(ActionFailedError):
    """An action exceeded its time budget."""

    code = "action_timeout"


class NavigationBlockedError(BrowserError):
    """Navigation was refused because the destination is outside the allowlist."""

    code = "navigation_blocked"

    def __init__(self, url: str, *, allowed: list[str] | None = None) -> None:
        super().__init__(
            f"Navigation to {url!r} is outside the allowed origins",
            url=url,
            allowed_origins=allowed or [],
        )


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------
class SafetyError(KarumiError):
    """An action was refused on safety grounds."""

    code = "safety_error"


class SensitiveActionError(SafetyError):
    """An action needs explicit human confirmation and did not have it."""

    code = "sensitive_action"

    def __init__(self, *, action: str, reason: str, label: str | None = None) -> None:
        super().__init__(
            f"Action {action!r} needs operator confirmation: {reason}",
            action=action,
            reason=reason,
            label=label,
        )
        self.reason = reason


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class ModelError(KarumiError):
    """A local model failed to load or produce a usable result."""

    code = "model_error"


class LLMError(ModelError):
    """The local LLM failed or returned something unusable."""

    code = "llm_error"
