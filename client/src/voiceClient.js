import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

export const PIPECAT_WEBRTC_URL =
  import.meta.env.VITE_PIPECAT_WEBRTC_URL ?? "http://localhost:7860/api/offer";

export const voiceClient = new PipecatClient({
  transport: new SmallWebRTCTransport(),
  enableMic: true,
  enableCam: false,
});
