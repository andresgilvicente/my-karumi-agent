import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  usePipecatClient,
  usePipecatClientMicControl,
  usePipecatClientTransportState,
  useRTVIClientEvent,
} from "@pipecat-ai/client-react";
import { useCallback, useState } from "react";
import { PIPECAT_WEBRTC_URL } from "./voiceClient.js";

const CONNECTED_STATES = new Set(["connected", "ready"]);
const BUSY_STATES = new Set(["initializing", "authenticating", "connecting", "disconnecting"]);

function errorMessage(error) {
  return error?.data?.error ?? error?.data?.message ?? error?.message ?? "Voice connection failed";
}

function VoiceControls() {
  const client = usePipecatClient();
  const transportState = usePipecatClientTransportState();
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();
  const [activity, setActivity] = useState("disconnected");
  const [error, setError] = useState("");

  const handleBotReady = useCallback(() => {
    setError("");
    setActivity("connected");
  }, []);
  const handleUserStartedSpeaking = useCallback(() => setActivity("listening"), []);
  const handleUserStoppedSpeaking = useCallback(() => setActivity("thinking"), []);
  const handleBotLlmStarted = useCallback(() => setActivity("thinking"), []);
  const handleBotStartedSpeaking = useCallback(() => setActivity("speaking"), []);
  const handleBotStoppedSpeaking = useCallback(() => setActivity("connected"), []);
  const handleDisconnected = useCallback(() => setActivity("disconnected"), []);
  const handleError = useCallback((nextError) => {
    setError(errorMessage(nextError));
    setActivity("error");
  }, []);

  useRTVIClientEvent(RTVIEvent.BotReady, handleBotReady);
  useRTVIClientEvent(RTVIEvent.UserStartedSpeaking, handleUserStartedSpeaking);
  useRTVIClientEvent(RTVIEvent.UserStoppedSpeaking, handleUserStoppedSpeaking);
  useRTVIClientEvent(RTVIEvent.BotLlmStarted, handleBotLlmStarted);
  useRTVIClientEvent(RTVIEvent.BotStartedSpeaking, handleBotStartedSpeaking);
  useRTVIClientEvent(RTVIEvent.BotStoppedSpeaking, handleBotStoppedSpeaking);
  useRTVIClientEvent(RTVIEvent.Disconnected, handleDisconnected);
  useRTVIClientEvent(RTVIEvent.Error, handleError);
  useRTVIClientEvent(RTVIEvent.DeviceError, handleError);

  const connected = CONNECTED_STATES.has(transportState);
  const busy = BUSY_STATES.has(transportState);
  const status = error
    ? "error"
    : busy
      ? transportState === "disconnecting"
        ? "disconnecting"
        : "connecting"
      : connected
        ? activity
        : transportState === "error"
          ? "error"
          : "disconnected";

  async function toggleConnection() {
    setError("");
    try {
      if (connected) {
        await client.disconnect();
      } else {
        setActivity("connecting");
        await client.connect({ webrtcUrl: PIPECAT_WEBRTC_URL });
      }
    } catch (nextError) {
      handleError(nextError);
    }
  }

  return (
    <div className="voice-controls" aria-label="Voice agent controls">
      <span className={`voice-status voice-status-${status}`} role="status" title={error || status}>
        <span aria-hidden="true"></span>
        {status}
      </span>
      <button
        className="voice-button"
        type="button"
        onClick={toggleConnection}
        disabled={busy}
      >
        {connected ? "Disconnect" : "Connect voice"}
      </button>
      <button
        className="voice-button voice-button-secondary"
        type="button"
        onClick={() => enableMic(!isMicEnabled)}
        disabled={!connected}
        aria-pressed={!isMicEnabled}
      >
        {isMicEnabled ? "Mute" : "Unmute"}
      </button>
    </div>
  );
}

export default VoiceControls;
