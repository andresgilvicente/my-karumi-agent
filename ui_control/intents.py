import re

from ui_control.catalog import UIControlCatalog


def _normalize(text: str) -> str:
    return " ".join(
        re.sub(r"[^\w\s']", " ", text.casefold().replace("’", "'")).split()
    )


def _phrase_index(text: str, phrase: str) -> int | None:
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"(?!\w)"
    match = re.search(pattern, text)
    return match.start() if match else None


def _first_phrase_index(text: str, phrases: list[str] | tuple[str, ...]) -> int | None:
    indexes = [_phrase_index(text, phrase) for phrase in phrases]
    indexes = [index for index in indexes if index is not None]
    return min(indexes) if indexes else None


def _requested_views(text: str, catalog: UIControlCatalog) -> dict[str, int]:
    matches = {}
    for view, definition in catalog.views.items():
        indexes = []
        for alias in definition["navigationAliases"]:
            index = _phrase_index(text, alias)
            if index is None:
                continue
            if alias == "tim":
                suffix = text[index + len(alias) :].strip()
                if suffix and not suffix.startswith(("section", "members", "member list")):
                    continue
            indexes.append(index)
        matches[view] = min(indexes) if indexes else None
    matches = {view: index for view, index in matches.items() if index is not None}
    return matches


def _requested_target(
    text: str,
    catalog: UIControlCatalog,
    preferred_view: str | None = None,
) -> tuple[str, int] | None:
    matches: list[tuple[str, int, int]] = []
    for target, definition in catalog.targets.items():
        for alias in definition["resolverAliases"]:
            index = _phrase_index(text, alias)
            if index is not None:
                matches.append((target, index, len(alias)))
    if not matches:
        return None

    strongest = []
    for match in matches:
        _, start, length = match
        end = start + length
        if any(
            other_start <= start
            and other_start + other_length >= end
            and other_length > length
            for _, other_start, other_length in matches
        ):
            continue
        strongest.append(match)
    targets = {target for target, _, _ in strongest}
    spans = {(start, length) for _, start, length in strongest}
    if len(targets) > 1 and len(spans) == 1 and preferred_view is not None:
        preferred_targets = {
            target
            for target in targets
            if catalog.targets[target]["view"] == preferred_view
        }
        if len(preferred_targets) == 1:
            targets = preferred_targets
            strongest = [match for match in strongest if match[0] in targets]
    if len(targets) != 1:
        return None
    target = targets.pop()
    return target, min(index for candidate, index, _ in strongest if candidate == target)


def _team_search_query(text: str, catalog: UIControlCatalog) -> str | None:
    for prefix in ("can you ", "could you ", "please "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    for template in catalog.data["teamSearchPhrases"]:
        prefix, suffix = template.split("{query}")
        pattern = "^" + re.escape(prefix).replace(r"\ ", r"\s+")
        pattern += r"(?P<query>.+?)"
        pattern += re.escape(suffix).replace(r"\ ", r"\s+") + "$"
        match = re.match(pattern, text)
        if match:
            query = match.group("query").strip(" ' ")
            if template == "search for {query}" and len(query.split()) != 1:
                return None
            reserved_queries = {
                alias.casefold()
                for definition in catalog.views.values()
                for alias in definition["navigationAliases"]
            }
            reserved_queries.update(
                alias.casefold()
                for definition in catalog.targets.values()
                for alias in definition["resolverAliases"]
            )
            query_words = set(query.split())
            blocked_words = set(catalog.data["nonPersonSearchTerms"])
            return (
                query.title()
                if query
                and query not in reserved_queries
                and query_words.isdisjoint(blocked_words)
                else None
            )
    return None


def _team_clear_search(text: str, catalog: UIControlCatalog) -> bool:
    for prefix in ("can you ", "could you ", "please "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text in catalog.data.get("clearSearchPhrases", ())


def resolve_ui_intent(
    text: str,
    catalog: UIControlCatalog,
    previous_text: str = "",
    current_view: str | None = None,
) -> tuple[str, dict] | None:
    """Resolve only explicit, allowlisted product actions."""
    current = _normalize(text)
    previous = _normalize(previous_text)

    if _team_clear_search(current, catalog):
        return "clear_team_search", {}

    search_query = _team_search_query(current, catalog)
    if search_query:
        return "search_team_member", {"query": search_query}

    requested_views = _requested_views(current, catalog)
    requested_view = (
        next(iter(requested_views.items())) if len(requested_views) == 1 else None
    )
    preferred_view = requested_view[0] if requested_view is not None else current_view
    requested_target = (
        _requested_target(current, catalog, preferred_view)
        if len(requested_views) <= 1
        else None
    )
    if (
        requested_target is not None
        and requested_view is not None
        and catalog.targets[requested_target[0]]["view"] != requested_view[0]
    ):
        return None
    scroll_index = _first_phrase_index(current, catalog.data["scrollPhrases"])
    if scroll_index is not None and requested_target is not None:
        target, target_index = requested_target
        if scroll_index < target_index and catalog.target_supports_command(target, "scroll_to"):
            return "scroll_to", {"target": target}

    focus_index = _first_phrase_index(current, catalog.data["focusPhrases"])
    if focus_index is not None and requested_target is not None:
        target, target_index = requested_target
        if focus_index < target_index:
            return "focus_target", {"target": target}

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


def fallback_tool_name(
    text: str,
    catalog: UIControlCatalog,
    current_view: str | None = None,
) -> str | None:
    """Keep a limited Qwen fallback without widening the allowlisted surface."""
    current = _normalize(text)
    requested_views = _requested_views(current, catalog)
    requested_view = (
        next(iter(requested_views.items())) if len(requested_views) == 1 else None
    )
    preferred_view = requested_view[0] if requested_view is not None else current_view
    requested_target = (
        _requested_target(current, catalog, preferred_view)
        if len(requested_views) <= 1
        else None
    )
    if (
        requested_target is not None
        and requested_view is not None
        and catalog.targets[requested_target[0]]["view"] != requested_view[0]
    ):
        return None

    informational_index = _first_phrase_index(
        current,
        ("what", "who", "why", "how", "when", "tell me", "explain", "describe"),
    )

    def action_is_explicit(action_index: int | None) -> bool:
        return action_index is not None and (
            informational_index is None or action_index < informational_index
        )

    if requested_target is not None:
        if action_is_explicit(
            _first_phrase_index(current, catalog.data["scrollPhrases"])
        ):
            return "scroll_to"
        if action_is_explicit(
            _first_phrase_index(current, catalog.data["focusPhrases"])
        ):
            return "focus_target"
    if requested_view is not None and action_is_explicit(
        _first_phrase_index(current, catalog.data["fallbackNavigationPhrases"])
    ):
        return "navigate_to"
    return None
