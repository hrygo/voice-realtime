import StatusBar from "./components/StatusBar";
import SubtitleStream from "./components/SubtitleStream";
import "./App.css";

export default function App() {
  return (
    <>
      <StatusBar />
      <main className="app-main">
        <section className="panel assistant-panel">
          <header className="panel-header">
            <h2>语音助手</h2>
            <span className="panel-hint">M3 接入状态桥</span>
          </header>
          <div className="panel-body placeholder-text">语音助手面板建设中…</div>
        </section>
        <SubtitleStream />
      </main>
    </>
  );
}
