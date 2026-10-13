// Phase 0: leere Seite. Heatmap-Rendering folgt in Phase 2.
import "./style.css";

const app = document.querySelector<HTMLDivElement>("#app");
if (!app) throw new Error("#app fehlt in index.html");
