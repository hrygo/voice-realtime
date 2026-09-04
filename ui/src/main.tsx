import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyTheme, initialTheme, initThemeListener } from "./stores/uiSettingsStore";
import "./index.css";

// 首帧渲染前同步设置 data-theme，避免亮/暗闪烁（FOUC）。
applyTheme(initialTheme());
initThemeListener();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
