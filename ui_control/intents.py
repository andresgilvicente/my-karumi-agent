from ui_control.catalog import UIControlCatalog


def _first_phrase_index(text: str, phrases: list[str] | tuple[str, ...]) -> int | None:
    indexes = [text.find(phrase) for phrase in phrases if phrase in text]
    return min(indexes) if indexes else None


def _requested_view(text: str, catalog: UIControlCatalog) -> tuple[str, int] | None:
    matches = {
        view: _first_phrase_index(text, definition["aliases"])
        for view, definition in catalog.views.items()
    }
    matches = {view: index for view, index in matches.items() if index is not None}
    if len(matches) != 1:
        return None
    return next(iter(matches.items()))


def resolve_ui_intent(
    text: str,
    catalog: UIControlCatalog,
    previous_text: str = "",
) -> tuple[str, dict] | None:
    """Resolve only the catalog's explicit, allowlisted semantic UI actions."""
    current = text.casefold().strip()
    previous = previous_text.casefold().strip()
    requested_view = _requested_view(current, catalog)

    for target, definition in catalog.targets.items():
        if definition["command"] == "scroll_to" and _first_phrase_index(
            current, catalog.data["scrollPhrases"]
        ) is not None and _first_phrase_index(current, definition["resolverAliases"]) is not None:
            return "scroll_to", {"target": target}

    focus_index = _first_phrase_index(current, catalog.data["focusPhrases"])
    if focus_index is not None and requested_view is not None:
        view, target_index = requested_view
        if focus_index < target_index:
            default_action = catalog.views[view]["defaultAction"]
            if default_action["command"] == "highlight":
                return default_action["command"], {"target": default_action["target"]}

    if requested_view is None:
        return None

    view, target_index = requested_view
    navigation_index = _first_phrase_index(current, catalog.data["navigationPhrases"])
    continued_navigation = (
        _first_phrase_index(previous, catalog.data["navigationPhrases"]) is not None
        and current.startswith(("to ", "into "))
    )
    if (navigation_index is not None and navigation_index < target_index) or continued_navigation:
        return "navigate_to", {"view": view}

    return None


def fallback_tool_name(text: str, catalog: UIControlCatalog) -> str | None:
    """Keep the pre-existing limited Qwen fallback without widening its surface."""
    current = text.casefold()
    fallback_targets = ["there"]
    for definition in catalog.views.values():
        fallback_targets.extend(definition["aliases"])

    if _first_phrase_index(current, catalog.data["scrollPhrases"]) is not None:
        return "scroll_to"
    if _first_phrase_index(current, catalog.data["focusPhrases"][:2]) is not None:
        return "highlight"
    if (
        _first_phrase_index(current, catalog.data["fallbackNavigationPhrases"]) is not None
        and _first_phrase_index(current, fallback_targets) is not None
    ):
        return "navigate_to"
    return None
