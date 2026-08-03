"""Action policy.

These tests encode the safety promises the README makes. They are written
against structural properties — roles, input types, accessible names — rather
than against any particular page, because that is how the policy has to work on
a site nobody has seen.
"""

from __future__ import annotations

import pytest

from karumi_agent.domain.models import (
    Action,
    ActionKind,
    Capability,
    ElementState,
    ErrorCode,
)
from karumi_agent.tools.policy import READ_ONLY, ActionPolicy


def action(kind: ActionKind, **kwargs: object) -> Action:
    return Action(action_id="a1", kind=kind, snapshot_id="s1", **kwargs)  # type: ignore[arg-type]


def element(**kwargs: object) -> ElementState:
    base: dict[str, object] = {"ref": "f0.e1", "tag": "button", "name": "OK"}
    base.update(kwargs)
    return ElementState.model_validate(base)


class TestCapabilities:
    def test_read_only_policy_refuses_clicks(self) -> None:
        decision = READ_ONLY.check(action(ActionKind.CLICK, ref="f0.e1"), element())

        assert not decision.allowed
        assert decision.error_code is ErrorCode.BLOCKED_BY_POLICY

    def test_read_only_policy_allows_pointing(self) -> None:
        for kind in (ActionKind.SCROLL_TO, ActionKind.HIGHLIGHT, ActionKind.SELECT_TEXT):
            assert READ_ONLY.check(action(kind, ref="f0.e1"), element()).allowed

    def test_fill_requires_the_fill_capability(self) -> None:
        policy = ActionPolicy(capabilities=frozenset({Capability.CLICK}))
        decision = policy.check(
            action(ActionKind.FILL, ref="f0.e1", value="x"), element(tag="input")
        )
        assert decision.error_code is ErrorCode.BLOCKED_BY_POLICY


class TestSensitiveActions:
    @pytest.mark.parametrize(
        "name",
        [
            "Delete all records",
            "Remove workspace",
            "Buy now",
            "Complete purchase",
            "Publish to production",
            "Borrar cuenta",
            "Eliminar informe",
            "Comprar ahora",
        ],
    )
    def test_destructive_names_need_confirmation(self, name: str) -> None:
        decision = ActionPolicy().check(action(ActionKind.CLICK, ref="f0.e1"), element(name=name))

        assert not decision.allowed
        assert decision.needs_confirmation
        assert decision.error_code is ErrorCode.NEEDS_CONFIRMATION

    def test_confirmation_can_be_downgraded_to_a_hard_block(self) -> None:
        policy = ActionPolicy(allow_sensitive_with_confirmation=False)
        decision = policy.check(
            action(ActionKind.CLICK, ref="f0.e1"), element(name="Delete everything")
        )

        assert decision.error_code is ErrorCode.BLOCKED_BY_POLICY
        assert not decision.needs_confirmation

    def test_submit_buttons_are_sensitive_regardless_of_name(self) -> None:
        decision = ActionPolicy().check(
            action(ActionKind.CLICK, ref="f0.e1"), element(name="Continue", type="submit")
        )
        assert decision.needs_confirmation

    def test_ordinary_controls_pass(self) -> None:
        for name in ("Analytics", "Show details", "Next page", "Siguiente"):
            assert ActionPolicy().check(
                action(ActionKind.CLICK, ref="f0.e1"), element(name=name)
            ).allowed

    def test_substring_matches_do_not_trigger(self) -> None:
        """`\\b` anchoring keeps 'Deleted items' from reading as 'delete'."""
        assert ActionPolicy().check(
            action(ActionKind.CLICK, ref="f0.e1"), element(name="Undeleted drafts")
        ).allowed

    def test_disabled_controls_are_refused(self) -> None:
        decision = ActionPolicy().check(
            action(ActionKind.CLICK, ref="f0.e1"), element(disabled=True)
        )
        assert decision.error_code is ErrorCode.ELEMENT_DISABLED


class TestFieldSafety:
    @pytest.mark.parametrize("field_type", ["password", "file", "hidden"])
    def test_forbidden_input_types_are_never_written(self, field_type: str) -> None:
        policy = ActionPolicy(capabilities=frozenset({Capability.FILL}))
        decision = policy.check(
            action(ActionKind.FILL, ref="f0.e1", value="secret"),
            element(tag="input", type=field_type, name="Anything at all"),
        )

        assert not decision.allowed
        assert decision.error_code is ErrorCode.BLOCKED_BY_POLICY

    def test_ordinary_text_fields_are_writable(self) -> None:
        policy = ActionPolicy(capabilities=frozenset({Capability.FILL}))
        decision = policy.check(
            action(ActionKind.FILL, ref="f0.e1", value="hello"),
            element(tag="input", type="text", name="Search"),
        )
        assert decision.allowed

    def test_non_fields_are_refused(self) -> None:
        policy = ActionPolicy(capabilities=frozenset({Capability.FILL}))
        decision = policy.check(
            action(ActionKind.FILL, ref="f0.e1", value="hello"), element(tag="div")
        )
        assert decision.error_code is ErrorCode.BLOCKED_BY_POLICY


class TestNavigationScope:
    def test_same_origin_navigation_is_allowed(self) -> None:
        policy = ActionPolicy().with_origin("https://app.example.test/dashboard")
        decision = policy.check(
            action(ActionKind.NAVIGATE, url="https://app.example.test/reports"), None
        )
        assert decision.allowed

    def test_cross_origin_navigation_is_blocked(self) -> None:
        policy = ActionPolicy().with_origin("https://app.example.test/dashboard")
        decision = policy.check(
            action(ActionKind.NAVIGATE, url="https://evil.example.com/"), None
        )

        assert not decision.allowed
        assert decision.error_code is ErrorCode.NAVIGATION_BLOCKED

    def test_non_http_schemes_are_blocked(self) -> None:
        """A page that offers `javascript:` or `file:` gets nowhere."""
        for url in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"):
            decision = ActionPolicy().check(action(ActionKind.NAVIGATE, url=url), None)
            assert decision.error_code is ErrorCode.NAVIGATION_BLOCKED, url

    def test_unpinned_policy_allows_any_http_origin(self) -> None:
        decision = ActionPolicy().check(
            action(ActionKind.NAVIGATE, url="https://anywhere.example/"), None
        )
        assert decision.allowed

    def test_going_back_is_always_allowed(self) -> None:
        policy = ActionPolicy().with_origin("https://app.example.test/")
        assert policy.check(action(ActionKind.GO_BACK), None).allowed
