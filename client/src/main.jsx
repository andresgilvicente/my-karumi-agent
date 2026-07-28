import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { PipecatClientAudio, PipecatClientProvider } from "@pipecat-ai/client-react";
import App from "./App.jsx";
import { voiceClient } from "./voiceClient.js";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <PipecatClientProvider client={voiceClient}>
      <App />
      <PipecatClientAudio />
    </PipecatClientProvider>
  </StrictMode>,
);
