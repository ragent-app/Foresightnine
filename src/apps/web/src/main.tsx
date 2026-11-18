import { createRoot } from "react-dom/client";

// The legacy Web UI was retired on 2026-11-18 and archived under
// src/docs/archive/web-ui-legacy-2026-11-18/. A new UI built on the
// native research runtime will replace this placeholder.
function Placeholder() {
  return <main style={{ fontFamily: "system-ui, sans-serif", padding: 24 }}>
    <h1>ForesightNine</h1>
    <p>新版研究界面正在重建。</p>
  </main>;
}

createRoot(document.getElementById("root")!).render(<Placeholder />);
