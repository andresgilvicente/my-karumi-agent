import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

export const PIPECAT_WEBRTC_URL =
  import.meta.env.VITE_PIPECAT_WEBRTC_URL ?? "http://localhost:7860/api/offer";

class SafeSmallWebRTCTransport extends SmallWebRTCTransport {
  sendIceCandidate(candidate) {
    // Chrome can emit an end-of-candidates object with an empty SDP candidate.
    if (!candidate?.candidate?.trim()) return;
    return super.sendIceCandidate(candidate);
  }
}

export const voiceClient = new PipecatClient({
  transport: new SafeSmallWebRTCTransport(),
  enableMic: true,
  enableCam: false,
});
