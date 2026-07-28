import { RTVIEvent } from "@pipecat-ai/client-js";
import {
  usePipecatClient,
  usePipecatClientMicControl,
  usePipecatClientTransportState,
  useRTVIClientEvent,
} from "@pipecat-ai/client-react";
import { useCallback, useRef, useState } from "react";
import { PIPECAT_WEBRTC_URL } from "./voiceClient.js";

const CONNECTED_STATES = new Set(["connected", "ready"]);
const BUSY_STATES = new Set(["initializing", "authenticating", "connecting", "disconnecting"]);
const DISCONNECT_SETTLE_MS = 1000;
const CONNECT_TIMEOUT_MS = 20000;

function errorMessage(error) {
  return error?.data?.error ?? error?.data?.message ?? error?.message ?? "Voice connection failed";
}

function VoiceControls() {
  const client = usePipecatClient();
  const transportState = usePipecatClientTransportState();
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();
  const [activity, setActivity] = useState("disconnected");
  const [error, setError] = useState("");
  const [operationPending, setOperationPending] = useState(false);
  const operationInProgressRef = useRef(false);
  const settlePromiseRef = useRef(null);
  const interruptConnectRef = useRef(null);
  const signalTransportConnectedRef = useRef(null);

  const beginDisconnectSettlement = useCallback(() => {
    if (settlePromiseRef.current) return settlePromiseRef.current;
    operationInProgressRef.current = true;
    setOperationPending(true);
    let settlement;
    settlement = new Promise((resolve) => {
      window.setTimeout(resolve, DISCONNECT_SETTLE_MS);
    }).finally(() => {
      if (settlePromiseRef.current !== settlement) return;
      settlePromiseRef.current = null;
      operationInProgressRef.current = false;
      setOperationPending(false);
    });
    settlePromiseRef.current = settlement;
    return settlement;
  }, []);

  const handleBotReady = useCallback(() => {
    setError("");
    setActivity("connected");
  }, []);
  const handleTransportConnected = useCallback(() => {
    signalTransportConnectedRef.current?.();
    signalTransportConnectedRef.current = null;
  }, []);
  const handleUserStartedSpeaking = useCallback(() => setActivity("listening"), []);
  const handleUserStoppedSpeaking = useCallback(() => setActivity("thinking"), []);
  const handleBotLlmStarted = useCallback(() => setActivity("thinking"), []);
  const handleBotStartedSpeaking = useCallback(() => setActivity("speaking"), []);
  const handleBotStoppedSpeaking = useCallback(() => setActivity("connected"), []);
  const handleDisconnected = useCallback(() => {
    setActivity("disconnected");
    interruptConnectRef.current?.(new Error("Voice connection ended before the bot was ready"));
    interruptConnectRef.current = null;
    void beginDisconnectSettlement();
  }, [beginDisconnectSettlement]);
  const handleError = useCallback((nextError) => {
    setError(errorMessage(nextError));
    setActivity("error");
    interruptConnectRef.current?.(nextError);
  }, []);

  useRTVIClientEvent(RTVIEvent.BotReady, handleBotReady);
  useRTVIClientEvent(RTVIEvent.Connected, handleTransportConnected);
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
    if (operationInProgressRef.current) return;
    operationInProgressRef.current = true;
    setOperationPending(true);
    setError("");
    try {
      if (connected) {
        await client.disconnect();
        await beginDisconnectSettlement();
      } else {
        setActivity("connecting");
        let timeoutId;
        let signalTransportConnected;
        let connectFinished = false;
        const transportConnected = new Promise((resolve) => {
          signalTransportConnected = resolve;
          signalTransportConnectedRef.current = resolve;
        });
        const interrupted = new Promise((_, reject) => {
          interruptConnectRef.current = reject;
        });
        const botReadyTimeout = transportConnected.then(
          () =>
            new Promise((_, reject) => {
              if (connectFinished) return;
              timeoutId = window.setTimeout(
                () => reject(new Error("Voice connection timed out")),
                CONNECT_TIMEOUT_MS,
              );
            }),
        );
        try {
          await Promise.race([
            client.connect({ webrtcUrl: PIPECAT_WEBRTC_URL }),
            interrupted,
            botReadyTimeout,
          ]);
        } finally {
          connectFinished = true;
          window.clearTimeout(timeoutId);
          interruptConnectRef.current = null;
          if (signalTransportConnectedRef.current === signalTransportConnected) {
            signalTransportConnectedRef.current = null;
          }
        }
      }
    } catch (nextError) {
      handleError(nextError);
      try {
        await client.disconnect();
      } catch {
        // Continue through the same settling window even if transport cleanup reports an error.
      }
      await beginDisconnectSettlement();
    } finally {
      if (settlePromiseRef.current) {
        await settlePromiseRef.current;
      } else {
        operationInProgressRef.current = false;
        setOperationPending(false);
      }
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
        disabled={busy || operationPending}
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
