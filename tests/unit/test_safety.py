"""Sensitivity classification.

These tests pin the two properties that matter: dangerous controls are caught
from their label alone (in several languages, with no selectors anywhere), and
merely *talking about* a dangerous control stays cheap.
"""

from __future__ import annotations

import pytest

from karumi_agent.domain.actions import ActionKind
from karumi_agent.domain.safety import (
    Sensitivity,
    classify_action,
    classify_label,
    is_credential_field,
    redact,
)
from karumi_agent.domain.snapshot import SnapshotNode


class TestLabelClassification:
    @pytest.mark.parametrize(
        "label",
        [
            "Delete workspace",
            "Remove member",
            "Eliminar cuenta",
            "Supprimer le projet",
            "Konto löschen",
            "Excluir item",
        ],
    )
    def test_destructive_labels_need_confirmation(self, label: str) -> None:
        assert classify_label(label).sensitivity is Sensitivity.CONFIRM

    @pytest.mark.parametrize(
        "label",
        ["Buy now", "Proceed to checkout", "Pay invoice", "Comprar", "Upgrade plan"],
    )
    def test_financial_labels_need_confirmation(self, label: str) -> None:
        assert classify_label(label).sensitivity is Sensitivity.CONFIRM

    @pytest.mark.parametrize("label", ["Sign out", "Log out", "Cerrar sesión", "Change password"])
    def test_account_labels_need_confirmation(self, label: str) -> None:
        assert classify_label(label).sensitivity is Sensitivity.CONFIRM

    @pytest.mark.parametrize(
        "label",
        ["Dashboard", "Analytics", "Overview", "Revenue", "Next page", "Filter by role"],
    )
    def test_ordinary_navigation_is_safe(self, label: str) -> None:
        assert classify_label(label).sensitivity is Sensitivity.SAFE

    def test_word_boundaries_prevent_false_positives(self) -> None:
        """'Payments overview' is a report, not a payment."""
        assert classify_label("Undeleted items").sensitivity is Sensitivity.SAFE
        assert classify_label("Deposit history").sensitivity is Sensitivity.SAFE

    def test_empty_label_is_safe(self) -> None:
        assert classify_label("").sensitivity is Sensitivity.SAFE


class TestActionClassification:
    def _button(self, name: str, **kwargs: object) -> SnapshotNode:
        return SnapshotNode(role="button", ref="e1", name=name, **kwargs)  # type: ignore[arg-type]

    def test_reading_about_a_dangerous_control_is_safe(self) -> None:
        """Highlighting "Delete" is how you talk about it during a demo.

        Blocking read-only actions on dangerous controls would make the agent
        useless without making it safer.
        """
        node = self._button("Delete workspace")
        for kind in (ActionKind.HIGHLIGHT, ActionKind.SCROLL, ActionKind.ANSWER):
            assert classify_action(kind, node).sensitivity is Sensitivity.SAFE

    def test_clicking_a_dangerous_control_needs_confirmation(self) -> None:
        node = self._button("Delete workspace")
        verdict = classify_action(ActionKind.CLICK, node)
        assert verdict.sensitivity is Sensitivity.CONFIRM
        assert "delete" in verdict.reason.lower()

    def test_clicking_an_ordinary_control_is_safe(self) -> None:
        assert classify_action(ActionKind.CLICK, self._button("Export")).sensitivity is (
            Sensitivity.CONFIRM
        )  # export moves data off the page, so it is gated
        assert classify_action(ActionKind.CLICK, self._button("Next")).sensitivity is (
            Sensitivity.SAFE
        )

    def test_filling_a_password_field_is_blocked_outright(self) -> None:
        node = SnapshotNode(role="textbox", ref="e1", name="Password")
        verdict = classify_action(ActionKind.FILL, node)
        assert verdict.sensitivity is Sensitivity.BLOCKED
        # Blocked means blocked: no confirmation can unlock it.
        assert verdict.is_blocked

    def test_dom_password_type_blocks_even_without_a_telling_name(self) -> None:
        node = SnapshotNode(role="textbox", ref="e1", name="Your secret code")
        verdict = classify_action(ActionKind.FILL, node, dom_is_password=True)
        assert verdict.sensitivity is Sensitivity.BLOCKED

    def test_leaving_the_origin_needs_confirmation(self) -> None:
        verdict = classify_action(ActionKind.NAVIGATE, None, leaves_origin=True)
        assert verdict.sensitivity is Sensitivity.CONFIRM

    def test_mutating_action_without_a_target_needs_confirmation(self) -> None:
        """If we cannot name it, we cannot ask about it, so we do not do it."""
        assert classify_action(ActionKind.CLICK, None).sensitivity is Sensitivity.CONFIRM

    def test_disabled_control_is_blocked(self) -> None:
        node = SnapshotNode(role="button", ref="e1", name="Save", states=("disabled",))
        assert classify_action(ActionKind.CLICK, node).sensitivity is Sensitivity.BLOCKED

    def test_new_tab_needs_confirmation(self) -> None:
        node = self._button("Docs")
        assert classify_action(ActionKind.CLICK, node, opens_new_tab=True).sensitivity is (
            Sensitivity.CONFIRM
        )


class TestCredentialDetection:
    @pytest.mark.parametrize(
        "name", ["Password", "Confirm password", "API key", "CVV", "Contraseña", "Senha"]
    )
    def test_detects_credential_fields_by_name(self, name: str) -> None:
        assert is_credential_field(SnapshotNode(role="textbox", name=name)) is True

    def test_detects_by_placeholder_via_name_field(self) -> None:
        node = SnapshotNode(role="textbox", name="", value="password")
        assert is_credential_field(node) is True

    def test_ordinary_field_is_not_a_credential(self) -> None:
        assert is_credential_field(SnapshotNode(role="textbox", name="Email")) is False

    def test_none_is_not_a_credential(self) -> None:
        assert is_credential_field(None) is False


class TestRedaction:
    def test_redacts_credential_assignments(self) -> None:
        assert "hunter2" not in redact("password: hunter2")
        assert "sk-abc123" not in redact("api_key=sk-abc123")

    def test_leaves_ordinary_text_alone(self) -> None:
        assert redact("Revenue is $128,430") == "Revenue is $128,430"


class TestConfirmationPrompt:
    def test_prompt_names_the_control(self) -> None:
        verdict = classify_label("Delete workspace")
        prompt = verdict.confirmation_prompt("Delete workspace")
        assert "Delete workspace" in prompt
        assert prompt.endswith("?")
