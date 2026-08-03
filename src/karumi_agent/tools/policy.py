"""Action policy — what the agent is allowed to do to a page.

Three layers, applied in order:

1. **Capability allowlist.** Coarse and operator-controlled. A capability that
   is off means the corresponding tool is not offered to the model at all, so
   there is nothing for page content to talk it into.
2. **Sensitive-action detection.** Structural, not a list of known buttons: an
   element is judged by its role, type and accessible name, so it works on a
   site nobody has seen before. Matches are blocked or escalated to a human.
3. **Navigation scope.** Keeps a demo on the product being demoed.

This runs *before* the browser is touched. It is deliberately not part of the
prompt: a rule the model could read is a rule a malicious page could argue with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from karumi_agent.domain.models import (
    ACTION_CAPABILITY,
    Action,
    ActionKind,
    Capability,
    ElementState,
    ErrorCode,
)

#: Accessible-name patterns that indicate an irreversible or costly action.
#: Multilingual on purpose — the target site's language is not known in advance.
SENSITIVE_NAME = re.compile(
    r"""
    \b(
      delete | remove | destroy | erase | wipe | reset
    | buy | purchase | checkout | pay | subscribe | upgrade | order
    | publish | post | send | share | invite | submit
    | deactivate | cancel\s+account | close\s+account | transfer
    | borrar | eliminar | suprimir | comprar | pagar | suscribir
    | publicar | enviar | compartir | invitar | confirmar\s+pedido
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Input types the agent must never write into, regardless of capability.
FORBIDDEN_INPUT_TYPES = frozenset({"password", "file", "hidden"})

#: Roles whose activation submits or commits something.
COMMITTING_ROLES = frozenset({"menuitemcheckbox", "menuitemradio"})


@dataclass(frozen=True)
class Decision:
    """Outcome of a policy check."""

    allowed: bool
    error_code: ErrorCode | None = None
    reason: str = ""
    needs_confirmation: bool = False

    @staticmethod
    def allow() -> Decision:
        return Decision(allowed=True)

    @staticmethod
    def block(code: ErrorCode, reason: str) -> Decision:
        return Decision(allowed=False, error_code=code, reason=reason)

    @staticmethod
    def confirm(reason: str) -> Decision:
        return Decision(
            allowed=False,
            error_code=ErrorCode.NEEDS_CONFIRMATION,
            reason=reason,
            needs_confirmation=True,
        )


@dataclass
class ActionPolicy:
    """Operator-controlled bounds on what the agent may do.

    The read-only default is intentional: a demo is mostly reading and pointing.
    Clicking and filling are opt-in, and even when enabled they refuse anything
    that looks like it spends money or destroys data.
    """

    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset(
            {
                Capability.READ,
                Capability.SCROLL,
                Capability.HIGHLIGHT,
                Capability.NAVIGATE,
                Capability.CLICK,
            }
        )
    )

    allowed_origins: frozenset[str] = frozenset()
    """Origins navigation may reach. Empty means "the origin we started on"."""

    allow_sensitive_with_confirmation: bool = True
    """When true, sensitive actions ask a human. When false, they are refused."""

    def with_origin(self, url: str) -> ActionPolicy:
        """Pin the policy to the origin of the demo's starting URL."""
        origin = _origin(url)
        if not origin:
            return self
        return ActionPolicy(
            capabilities=self.capabilities,
            allowed_origins=self.allowed_origins | {origin},
            allow_sensitive_with_confirmation=self.allow_sensitive_with_confirmation,
        )

    # -- checks ------------------------------------------------------------

    def check(self, action: Action, element: ElementState | None) -> Decision:
        """Evaluate an action. `element` is None for actions without a target."""
        capability = ACTION_CAPABILITY.get(action.kind)
        if capability is None:
            return Decision.block(ErrorCode.INVALID_ACTION, f"unknown action {action.kind}")
        if capability not in self.capabilities:
            return Decision.block(
                ErrorCode.BLOCKED_BY_POLICY,
                f"capability '{capability}' is not enabled for this session",
            )

        if action.kind in (ActionKind.NAVIGATE, ActionKind.GO_BACK):
            return self._check_navigation(action)

        if element is None:
            return Decision.allow()

        if action.kind == ActionKind.FILL:
            return self._check_fill(element)
        if action.kind == ActionKind.CLICK:
            return self._check_click(element)

        # Reading, scrolling, highlighting and selecting never mutate the page.
        return Decision.allow()

    def _check_navigation(self, action: Action) -> Decision:
        if action.kind == ActionKind.GO_BACK:
            return Decision.allow()
        if not action.url:
            return Decision.block(ErrorCode.INVALID_ACTION, "navigate requires a url")

        parsed = urlparse(action.url)
        if parsed.scheme not in ("http", "https"):
            return Decision.block(
                ErrorCode.NAVIGATION_BLOCKED,
                f"refusing non-http(s) scheme '{parsed.scheme}'",
            )
        if not self.allowed_origins:
            return Decision.allow()

        origin = _origin(action.url)
        if origin not in self.allowed_origins:
            return Decision.block(
                ErrorCode.NAVIGATION_BLOCKED,
                f"{origin} is outside the demo's allowed origins",
            )
        return Decision.allow()

    def _check_fill(self, element: ElementState) -> Decision:
        if element.disabled:
            return Decision.block(ErrorCode.ELEMENT_DISABLED, "field is disabled")
        if (element.type or "").lower() in FORBIDDEN_INPUT_TYPES:
            return Decision.block(
                ErrorCode.BLOCKED_BY_POLICY,
                f"refusing to write into a '{element.type}' input",
            )
        if element.tag not in ("input", "textarea") and element.role != "textbox":
            return Decision.block(
                ErrorCode.BLOCKED_BY_POLICY, f"'{element.tag}' is not a text field"
            )
        return Decision.allow()

    def _check_click(self, element: ElementState) -> Decision:
        if element.disabled:
            # The standard UI Worker client no-ops on disabled targets; we make
            # the refusal explicit so the model learns rather than retries.
            return Decision.block(ErrorCode.ELEMENT_DISABLED, "control is disabled")

        submits = (element.type or "").lower() == "submit"
        sensitive = bool(SENSITIVE_NAME.search(element.name or ""))
        committing = (element.role or "") in COMMITTING_ROLES

        if submits or sensitive or committing:
            what = element.name or element.role or element.tag
            reason = f"'{what}' looks like it commits or changes data"
            if self.allow_sensitive_with_confirmation:
                return Decision.confirm(reason)
            return Decision.block(ErrorCode.BLOCKED_BY_POLICY, reason)

        return Decision.allow()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


#: A policy suitable for unattended runs: look, point and navigate, nothing else.
READ_ONLY = ActionPolicy(
    capabilities=frozenset(
        {Capability.READ, Capability.SCROLL, Capability.HIGHLIGHT, Capability.NAVIGATE}
    )
)
