import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "../../../../packages/app-platform/src/theme.css";
import "./styles.css";
import { App } from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("JELICA Desktop renderer root is missing.");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
