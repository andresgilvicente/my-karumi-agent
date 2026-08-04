"""Sensitivity classification for page controls.

The trick that keeps this generic: **classify from the accessible name the model
already reasoned over.** No selectors, no element ids, no per-product allowlists.
If a button is labelled "Delete account", that string is in the snapshot, so the
same text that told the model what the control does tells us how dangerous it is.
It works in any language we have patterns for, and it stays honest when a page
relabels a control, because there is nothing cached to go stale.

This is a guardrail, not a proof. It is one layer among several (closed action
vocabulary, origin allowlist, no JS execution, confirmation gate); none of them is
load-bearing alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from karumi_agent.domain.actions import ActionKind
from karumi_agent.domain.snapshot import SnapshotNode


class Sensitivity(StrEnum):
    """How much caution a control demands."""

    SAFE = "safe"
    #: Needs an explicit spoken confirmation from the operator.
    CONFIRM = "confirm"
    #: Never performed, regardless of confirmation.
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """The result of classifying an action against a target."""

    sensitivity: Sensitivity
    reason: str
    matched: str | None = None

    @property
    def allowed_without_confirmation(self) -> bool:
        return self.sensitivity is Sensitivity.SAFE

    @property
    def is_blocked(self) -> bool:
        return self.sensitivity is Sensitivity.BLOCKED

    def confirmation_prompt(self, label: str) -> str:
        """What the agent should ask the operator before proceeding."""
        return f'"{label}" looks like it {self.reason}. Do you want me to go ahead?'


# Verb families matched against accessible names. Multilingual because the agent
# is meant to work on arbitrary sites, and a Spanish "Eliminar" is exactly as
# destructive as an English "Delete". Patterns are word-boundary anchored so
# "Undelete" or "Payments overview" do not trip the destructive rule.
_DESTRUCTIVE = r"""
    delet|remov|discard|destroy|erase|wipe|purge|revoke|
    eliminar|borrar|suprimir|
    supprim|effacer|
    l[oö]schen|entfernen|
    excluir|apagar
"""

_FINANCIAL = r"""
    buy|purchase|checkout|pay|payment|subscribe|upgrade|billing|
    transfer|withdraw|donate|place\s+order|
    comprar|pagar|suscribir|
    acheter|payer|
    kaufen|bezahlen|
    comprar|pagamento
"""

_ACCOUNT = r"""
    sign\s*out|log\s*out|sign\s*in|log\s*in|
    change\s+password|reset\s+password|deactivate|close\s+account|
    cerrar\s+sesi|iniciar\s+sesi|
    d[ée]connexion|se\s+connecter|
    abmelden|anmelden
"""

_PUBLISH = r"""
    publish|deploy|send|submit|post\b|share|invite|broadcast|release|
    publicar|enviar|compartir|invitar|
    publier|envoyer|partager|
    ver[oö]ffentlichen|senden|teilen
"""

_DOWNLOAD = r"""
    download|export|
    descargar|exportar|
    t[ée]l[ée]charger|exporter|
    herunterladen|exportieren
"""

_FAMILIES: tuple[tuple[re.Pattern[str], Sensitivity, str], ...] = (
    (
        re.compile(rf"\b(?:{_DESTRUCTIVE})", re.IGNORECASE | re.VERBOSE),
        Sensitivity.CONFIRM,
        "deletes or removes something",
    ),
    (
        re.compile(rf"\b(?:{_FINANCIAL})", re.IGNORECASE | re.VERBOSE),
        Sensitivity.CONFIRM,
        "starts a payment or a purchase",
    ),
    (
        re.compile(rf"\b(?:{_ACCOUNT})", re.IGNORECASE | re.VERBOSE),
        Sensitivity.CONFIRM,
        "changes the account or the session",
    ),
    (
        re.compile(rf"\b(?:{_PUBLISH})", re.IGNORECASE | re.VERBOSE),
        Sensitivity.CONFIRM,
        "sends or publishes something",
    ),
    (
        re.compile(rf"\b(?:{_DOWNLOAD})", re.IGNORECASE | re.VERBOSE),
        Sensitivity.CONFIRM,
        "downloads or exports data",
    ),
)

# Roles that indicate a credential field. Typing into one is never allowed, and
# its value is never read into any prompt.
# Stems rather than whole words for the non-English entries: "contraseña" ends in
# a letter that `\w` matches, so a trailing `\b` after "contrase" would never fire.
# Anchored only at the start, which still keeps "Deposit" from matching "pin".
_CREDENTIAL_HINT = re.compile(
    r"\b(?:password|passphrase|pin\b|cvv|card\s*number|secret|token|api\s*key|"
    r"contrase\w*|clave\b|mot\s+de\s+passe|passwort|senha)",
    re.IGNORECASE,
)


def classify_label(label: str) -> SafetyVerdict:
    """Classify a control by its accessible name alone."""
    text = (label or "").strip()
    if not text:
        return SafetyVerdict(Sensitivity.SAFE, "has no label")

    for pattern, sensitivity, reason in _FAMILIES:
        match = pattern.search(text)
        if match:
            return SafetyVerdict(sensitivity, reason, matched=match.group(0))
    return SafetyVerdict(Sensitivity.SAFE, "looks like a normal control")


def is_credential_field(node: SnapshotNode | None, *, dom_is_password: bool = False) -> bool:
    """Is this a field we must never type into or read?

    ``dom_is_password`` comes from the real ``input[type=password]`` check in the
    browser layer. The accessible-name heuristic is a second net for fields that
    are password-like without the type attribute (custom PIN inputs, for example).
    """
    if dom_is_password:
        return True
    if node is None:
        return False
    return bool(_CREDENTIAL_HINT.search(node.name or "")) or bool(
        _CREDENTIAL_HINT.search(node.value or "")
    )


def classify_action(
    kind: ActionKind,
    node: SnapshotNode | None,
    *,
    dom_is_password: bool = False,
    opens_new_tab: bool = False,
    leaves_origin: bool = False,
) -> SafetyVerdict:
    """Classify an intended action against its target.

    Read-only actions stay cheap: highlighting or scrolling to a "Delete" button
    is how you *talk about* it during a demo, and blocking that would make the
    agent useless without making it safer. Only actions that can actually change
    something are gated.
    """
    if kind is ActionKind.FILL and is_credential_field(node, dom_is_password=dom_is_password):
        return SafetyVerdict(
            Sensitivity.BLOCKED,
            "is a credential field; the agent never types into one",
        )

    if kind.is_read_only:
        return SafetyVerdict(Sensitivity.SAFE, "does not change the page")

    if leaves_origin:
        return SafetyVerdict(
            Sensitivity.CONFIRM,
            "navigates away from the site being demonstrated",
        )

    # Navigation-family actions are identified by their destination, not by an
    # element, and the destination has already been checked against the origin
    # allowlist above. Moving around inside the site being demonstrated is the
    # ordinary case, so it must not demand confirmation every time.
    if kind in {ActionKind.NAVIGATE, ActionKind.BACK, ActionKind.FORWARD}:
        return SafetyVerdict(Sensitivity.SAFE, "moves within the allowed site")

    if node is None:
        # An element-targeted action whose target we cannot name is not something
        # we can describe to the operator, so it cannot be meaningfully confirmed
        # either. Refusing to guess is the point.
        return SafetyVerdict(
            Sensitivity.CONFIRM,
            "changes the page but has no identifiable label",
        )

    verdict = classify_label(node.label)
    if verdict.sensitivity is not Sensitivity.SAFE:
        return verdict

    if opens_new_tab:
        return SafetyVerdict(Sensitivity.CONFIRM, "opens a new tab")

    if "disabled" in node.states:
        return SafetyVerdict(Sensitivity.BLOCKED, "is disabled")

    return SafetyVerdict(Sensitivity.SAFE, "is a normal control")


def redact(text: str) -> str:
    """Strip anything that looks like a credential before it reaches a log.

    Cheap belt-and-braces for the path where page text ends up in a JSONL run log
    that gets shared for debugging.
    """
    return re.sub(
        r"(?i)\b(password|token|secret|api[_-]?key)\b\s*[:=]\s*\S+",
        r"\1=<redacted>",
        text,
    )
