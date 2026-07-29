import json
import os
import platform
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


PROFILE_SCHEMA_VERSION = 1
PROFILE_DIRECTORY = Path(__file__).resolve().parent.parent / "profiles"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LocalVoiceProfile:
    """Write opt-in, local JSONL events and correlate one active voice turn."""

    def __init__(self, path: Path, *, case_id: str | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = uuid4().hex
        self.session_id: str | None = None
        self._case_id = case_id
        self._file = path.open("a", encoding="utf-8")
        self._session_index = 0
        self._turn_index = 0
        self._turn: dict | None = None
        self._closed = False

    @property
    def current_turn_id(self) -> str | None:
        return self._turn["turn_id"] if self._turn else None

    def emit(self, event: str, **fields) -> None:
        if self._closed:
            return
        record = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "event": event,
            "ts_utc": _utc_timestamp(),
            "run_id": self.run_id,
            "session_id": self.session_id,
            **fields,
        }
        if self._case_id:
            record["case_id"] = self._case_id
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._file.flush()

    def start_run(self, config: dict) -> None:
        self.emit(
            "run_started",
            process_start=True,
            python=platform.python_version(),
            platform=platform.platform(),
            config=config,
        )

    def start_session(self) -> None:
        if self.session_id is not None:
            self.end_session("reconnected")
        self._session_index += 1
        self._turn_index = 0
        self.session_id = uuid4().hex
        self.emit(
            "session_started",
            session_index=self._session_index,
            process_start=self._session_index == 1,
        )

    def end_session(self, reason: str) -> None:
        if self.session_id is None:
            return
        self.finish_turn(reason if reason == "disconnected" else "session_ended")
        self.emit("session_ended", reason=reason)
        self.session_id = None

    def start_turn(self) -> None:
        if self.session_id is None:
            return
        if self._turn is not None:
            self.finish_turn("interrupted")
        self._turn_index += 1
        turn_id = uuid4().hex
        self._turn = {
            "turn_id": turn_id,
            "turn_index": self._turn_index,
            "phase": "first_user_turn" if self._turn_index == 1 else "later_user_turn",
            "resolution": None,
            "transcript_count": 0,
            "metrics_ms": {},
            "ui_outcomes": [],
        }
        self.emit(
            "turn_started",
            turn_id=turn_id,
            turn_index=self._turn_index,
            phase=self._turn["phase"],
        )

    def speech_ended(self, *, silence_confirmation_ms: float) -> None:
        if self._turn is None:
            return
        self.emit(
            "speech_ended",
            turn_id=self.current_turn_id,
            silence_confirmation_ms=round(silence_confirmation_ms, 3),
        )

    def logical_turn_closed(self, duration_ms: float) -> None:
        self._record_metric("speech_end_to_turn_close", duration_ms)
        self.emit(
            "logical_turn_closed",
            turn_id=self.current_turn_id,
            duration_ms=round(duration_ms, 3),
        )

    def transcription_received(self, text: str, duration_ms: float | None) -> None:
        if self._turn is None:
            return
        self._turn["transcript_count"] += 1
        if duration_ms is not None:
            self._record_metric("speech_end_to_transcription", duration_ms)
        self.emit(
            "transcription_received",
            turn_id=self.current_turn_id,
            transcript=text.strip(),
            text_chars=len(text.strip()),
            transcript_count=self._turn["transcript_count"],
            duration_ms=round(duration_ms, 3) if duration_ms is not None else None,
        )

    def resolution(self, resolution: str, details: dict | None = None) -> None:
        if self._turn is not None:
            self._turn["resolution"] = resolution
        self.emit(
            "intent_resolved",
            turn_id=self.current_turn_id,
            resolution=resolution,
            **(details or {}),
        )

    def service_metric(self, service: str, metric: str, duration_ms: float) -> None:
        self._record_metric(f"{service}_{metric}", duration_ms)
        self.emit(
            "service_metric",
            turn_id=self.current_turn_id,
            service=service,
            metric=metric,
            duration_ms=round(duration_ms, 3),
            attribution="active_turn",
        )

    def first_bot_audio(self, duration_ms: float, *, startup: bool) -> None:
        metric = "client_connected_to_first_audio" if startup else "speech_end_to_first_audio"
        if not startup:
            self._record_metric(metric, duration_ms)
        self.emit(
            "first_bot_audio",
            turn_id=None if startup else self.current_turn_id,
            kind="startup" if startup else "turn",
            duration_ms=round(duration_ms, 3),
        )

    def ui_action_started(
        self,
        action_id: str,
        command_id: str | None,
        command: str,
        payload: dict,
    ) -> None:
        self.emit(
            "ui_action_started",
            turn_id=self.current_turn_id,
            action_id=action_id,
            command_id=command_id,
            command=command,
            **_safe_action_payload(payload),
        )

    def ui_action_finished(
        self,
        action_id: str,
        command_id: str | None,
        command: str,
        payload: dict,
        outcome: str,
        duration_ms: float,
        code: str | None = None,
    ) -> None:
        result = {
            "action_id": action_id,
            "command_id": command_id,
            "command": command,
            "outcome": outcome,
            "code": code,
            "duration_ms": round(duration_ms, 3),
            **_safe_action_payload(payload),
        }
        if self._turn is not None:
            self._turn["ui_outcomes"].append(result)
        self.emit("ui_action_finished", turn_id=self.current_turn_id, **result)

    def bot_stopped_speaking(self, tts_total_ms: float | None = None) -> None:
        if tts_total_ms is not None:
            self._record_metric("tts_total", tts_total_ms)
        self.emit(
            "bot_stopped_speaking",
            turn_id=self.current_turn_id,
            tts_total_ms=round(tts_total_ms, 3) if tts_total_ms is not None else None,
        )
        self.finish_turn("complete")

    def finish_turn(self, outcome: str) -> None:
        if self._turn is None:
            return
        turn = self._turn
        self.emit(
            "turn_summary",
            turn_id=turn["turn_id"],
            turn_index=turn["turn_index"],
            phase=turn["phase"],
            outcome=outcome,
            resolution=turn["resolution"],
            transcript_count=turn["transcript_count"],
            metrics_ms=turn["metrics_ms"],
            ui_outcomes=turn["ui_outcomes"],
        )
        self._turn = None

    def close(self) -> None:
        if self._closed:
            return
        self.end_session("shutdown")
        self.emit("run_ended")
        self._file.close()
        self._closed = True

    def _record_metric(self, name: str, duration_ms: float) -> None:
        if self._turn is not None:
            self._turn["metrics_ms"][name] = round(duration_ms, 3)


def create_local_profile(environ: dict[str, str] | None = None) -> LocalVoiceProfile | None:
    environ = os.environ if environ is None else environ
    if environ.get("KARUMI_PROFILE") != "1":
        return None
    configured_path = environ.get("KARUMI_PROFILE_PATH")
    path = Path(configured_path).expanduser() if configured_path else _default_profile_path()
    return LocalVoiceProfile(path, case_id=environ.get("KARUMI_PROFILE_CASE"))


def _default_profile_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return PROFILE_DIRECTORY / f"karumi-profile-{stamp}-{os.getpid()}.jsonl"


def _safe_action_payload(payload: dict) -> dict:
    return {key: payload[key] for key in ("view", "target") if key in payload}
