import AssistantPanel from "./components/AssistantPanel";
import StatusBar from "./components/StatusBar";
import SubtitleStream from "./components/SubtitleStream";
import "./App.css";

export default function App() {
  return (
    <>
      <StatusBar />
      <main className="app-main">
        <AssistantPanel />
        <SubtitleStream />
      </main>
    </>
  );
}
