import json
from dataclasses import dataclass
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent.parent / "client" / "src" / "ui-demo-catalog.json"


@dataclass(frozen=True)
class UIControlCatalog:
    data: dict

    @property
    def contract_version(self) -> int:
        return self.data["uiContractVersion"]

    @property
    def commands(self) -> dict:
        return self.data["commands"]

    @property
    def views(self) -> dict:
        return self.data["views"]

    @property
    def targets(self) -> dict:
        return self.data["targets"]

    @property
    def view_ids(self) -> tuple[str, ...]:
        return tuple(self.views)

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(self.targets)

    def targets_for_command(self, command: str) -> tuple[str, ...]:
        return tuple(
            target_id
            for target_id, target in self.targets.items()
            if target["command"] == command
        )

    def required_capabilities_for(self, command: str, payload: dict) -> dict:
        capabilities = {"commands": {command}, "views": set(), "targets": set()}
        if command == "navigate_to":
            view = payload.get("view")
            if view in self.views:
                capabilities["views"].add(view)
        else:
            target = payload.get("target")
            if target in self.targets:
                capabilities["targets"].add(target)
                capabilities["views"].add(self.targets[target]["view"])
        return capabilities

    def is_contract_compatible(self, state: dict) -> bool:
        if state.get("uiContractVersion") != self.contract_version:
            return False
        for key, required in (
            ("supportedCommands", set(self.commands)),
            ("supportedViews", set(self.views)),
            ("supportedTargets", set(self.targets)),
        ):
            announced = state.get(key)
            if not isinstance(announced, list) or not required.issubset(announced):
                return False
        return True

    def supports_action(self, state: dict | None, command: str, payload: dict) -> bool:
        if state is None or not state.get("contractCompatible"):
            return False
        capabilities = self.required_capabilities_for(command, payload)
        return (
            capabilities["commands"].issubset(state.get("supportedCommands", []))
            and capabilities["views"].issubset(state.get("supportedViews", []))
            and capabilities["targets"].issubset(state.get("supportedTargets", []))
        )

    def confirmation_matches(self, command: str, payload: dict, result: dict) -> bool:
        if result.get("status") != "ok":
            return True
        if result.get("command") != command:
            return False
        if command == "navigate_to":
            return result.get("currentPage") == payload.get("view")
        target = payload.get("target")
        definition = self.targets.get(target)
        return (
            definition is not None
            and result.get("currentPage") == definition["view"]
            and result.get("target") == target
        )


def load_catalog(path: Path = CATALOG_PATH) -> UIControlCatalog:
    with path.open(encoding="utf-8") as catalog_file:
        data = json.load(catalog_file)
    validate_catalog(data)
    return UIControlCatalog(data)


def validate_catalog(data: dict) -> None:
    if not isinstance(data.get("uiContractVersion"), int):
        raise ValueError("uiContractVersion must be an integer")

    commands = data.get("commands")
    views = data.get("views")
    targets = data.get("targets")
    if not isinstance(commands, dict) or not isinstance(views, dict) or not isinstance(targets, dict):
        raise ValueError("catalog must define commands, views, and targets")

    for command, definition in commands.items():
        if not isinstance(definition, dict) or definition.get("payloadKey") not in {"view", "target"}:
            raise ValueError(f"command {command} must define a supported payloadKey")

    for view, definition in views.items():
        default_action = definition.get("defaultAction", {})
        if not isinstance(definition.get("hash"), str) or not definition["hash"].startswith("#"):
            raise ValueError(f"view {view} must define a hash")
        if not isinstance(definition.get("elementId"), str):
            raise ValueError(f"view {view} must define an elementId")
        if not isinstance(definition.get("aliases"), list):
            raise ValueError(f"view {view} must define aliases")
        if default_action.get("command") not in commands or default_action.get("target") not in targets:
            raise ValueError(f"view {view} has an invalid defaultAction")

    for target, definition in targets.items():
        if definition.get("command") not in commands or definition.get("view") not in views:
            raise ValueError(f"target {target} has an invalid command or view")
        if not isinstance(definition.get("resolverAliases"), list):
            raise ValueError(f"target {target} must define resolverAliases")
