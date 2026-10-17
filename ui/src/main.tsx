// Entry point. CSS is imported here, not in the components, so vitest never loads it.

import "@xyflow/react/dist/style.css";
import "./index.css";
import { ReactFlowProvider } from "@xyflow/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { captureToken } from "./api/client";
import App from "./App";

captureToken(); // move #token=... from the URL fragment into sessionStorage before anything calls the API

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </StrictMode>,
);
