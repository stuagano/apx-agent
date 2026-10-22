import React from "react";
import ReactDOM from "react-dom/client";
import DiscoverApp from "./DiscoverApp";
import "@xyflow/react/dist/style.css";
import "../index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DiscoverApp />
  </React.StrictMode>,
);
