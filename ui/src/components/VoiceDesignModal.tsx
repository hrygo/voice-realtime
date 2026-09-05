import { useCallback, useEffect, useState } from "react";
import { apiUrl } from "../config/runtimeConfig";
import { playAudioBlob } from "../utils/audioPlayback";
import { showToast } from "./Toast";
import { SoundWaveAnimatedIcon } from "./Icons";
import type { VoiceCatalogItem } from "./assistantPresentation";
import "./VoiceDesignModal.css";

interface VoiceDesignModalProps {
  readonly onCancel: () => void;
  readonly onCreated: (voice: VoiceCatalogItem) => void;
}

interface InspirationPrompt {
  readonly label: string;
  readonly name: string;
  readonly instruction: string;
}

const INSPIRATIONS: readonly InspirationPrompt[] = [
  {
    label: "🌸 温柔知性",
    name: "知性女声",
    instruction: "温柔轻快、语调柔和的年轻女声，吐字清晰亲和，富有同理心与治愈感。",
  },
  {
    label: "💼 干练职场",
    name: "职场播报",
    instruction: "咬字精准、节奏从容稳健的成熟女性声音，适合新闻播报、会议总结与专业技术讲解。",
  },
  {
    label: "🍵 磁性男声",
    name: "磁性电台",
    instruction: "磁性温润、低沉浑厚的青年男声，语调沉静从容，适合深度交流与夜间电台陪伴。",
  },
  {
    label: "⚡ 元气少年",
    name: "元气阳光",
    instruction: "清脆明快、朝气蓬勃的少年音色，充满热情活力，语速轻快流畅，适合趣味互动与日常闲聊。",
  },
  {
    label: "📜 京味评书",
    name: "说书先生",
    instruction: "经典北京评书艺人口吻，咬字顿挫有力，幽默诙谐，带有传统说书人的腔调感与感染力。",
  },
];

export function VoiceDesignModal({ onCancel, onCreated }: VoiceDesignModalProps) {
  const [name, setName] = useState("");
  const [instruction, setInstruction] = useState("");
  const [previewText, setPreviewText] = useState("你好呀，我是你刚刚设计的专属音色，很高兴与你实时对话。");
  const [isPlayingPreview, setIsPlayingPreview] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !isSubmitting) {
        onCancel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onCancel, isSubmitting]);

  const handleApplyInspiration = useCallback((item: InspirationPrompt) => {
    setName(item.name);
    setInstruction(item.instruction);
    setErrorMsg("");
  }, []);

  const handlePreview = useCallback(async () => {
    if (!instruction.trim()) {
      setErrorMsg("请先输入音色特征描述，或选择上方灵感胶囊");
      return;
    }
    setErrorMsg("");
    setIsPlayingPreview(true);
    try {
      const resp = await fetch(apiUrl("/v1/audio/speech"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "speechrail/qwen3-tts",
          input: previewText.trim() || "你好，很高兴与你对话。",
          instruction: instruction.trim(),
        }),
      });
      if (!resp.ok) {
        throw new Error(`试听生成失败 (HTTP ${resp.status})`);
      }
      const blob = await resp.blob();
      await playAudioBlob(blob);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "语音试听生成失败，请检查服务状态";
      setErrorMsg(msg);
      showToast(msg, "error");
    } finally {
      setIsPlayingPreview(false);
    }
  }, [instruction, previewText]);

  const handleSubmit = useCallback(async () => {
    const trimmedName = name.trim();
    const trimmedInstruction = instruction.trim();

    if (!trimmedName) {
      setErrorMsg("请输入音色名称");
      return;
    }
    if (!trimmedInstruction) {
      setErrorMsg("请输入音色特征描述");
      return;
    }

    setErrorMsg("");
    setIsSubmitting(true);

    try {
      const resp = await fetch(apiUrl("/v1/voices"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: trimmedName,
          instruction: trimmedInstruction,
        }),
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || `创建音色失败 (HTTP ${resp.status})`);
      }

      const createdVoice = (await resp.json()) as VoiceCatalogItem;
      showToast(`专属音色「${createdVoice.name}」已创建并应用`, "success");
      onCreated(createdVoice);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "创建音色失败，请重试";
      setErrorMsg(msg);
      showToast(msg, "error");
    } finally {
      setIsSubmitting(false);
    }
  }, [name, instruction, onCreated]);

  return (
    <div
      className="voice-design-modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="voice-design-modal-title"
      onClick={onCancel}
    >
      <div className="voice-design-modal-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="voice-design-header">
          <h3 id="voice-design-modal-title">
            <span className="voice-design-title-icon">✨</span> 自然语言音色设计
          </h3>
          <button type="button" className="voice-design-close" onClick={onCancel} aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="voice-design-body">
          <div className="voice-design-field">
            <label htmlFor="voice-design-name" className="voice-design-label">
              音色名称 <span className="voice-design-required">*</span>
            </label>
            <input
              id="voice-design-name"
              type="text"
              className="voice-design-input"
              placeholder="为音色起一个好听的名字，如「知性姐姐」「元气少年」..."
              value={name}
              maxLength={24}
              onChange={(e) => {
                setName(e.target.value);
                if (errorMsg) setErrorMsg("");
              }}
            />
          </div>

          <div className="voice-design-field">
            <div className="voice-design-field-header">
              <label htmlFor="voice-design-desc" className="voice-design-label">
                音色特征描述 (自然语言 Prompt) <span className="voice-design-required">*</span>
              </label>
              <span className="voice-design-counter">{instruction.length} / 200</span>
            </div>
            <textarea
              id="voice-design-desc"
              className="voice-design-textarea"
              placeholder="用自然语言详细描述音色特征：性别、年龄、音质、语速、语调及情绪。例如：温和清澈的年轻女声，语调柔和缓慢，富有同理心..."
              rows={3}
              maxLength={200}
              value={instruction}
              onChange={(e) => {
                setInstruction(e.target.value);
                if (errorMsg) setErrorMsg("");
              }}
            />
          </div>

          <div className="voice-design-field">
            <span className="voice-design-sublabel">灵感预设库 (点击直接套用)：</span>
            <div className="voice-inspirations-grid">
              {INSPIRATIONS.map((item) => {
                const isActive = instruction.trim() === item.instruction.trim();
                return (
                  <button
                    key={item.label}
                    type="button"
                    className={`voice-inspiration-chip ${isActive ? "active" : ""}`}
                    onClick={() => handleApplyInspiration(item)}
                  >
                    {item.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="voice-design-field voice-preview-box">
            <div className="voice-preview-header">
              <label htmlFor="voice-preview-text" className="voice-design-sublabel">
                即时试听文本
              </label>
              <button
                type="button"
                className={`btn-voice-design-preview ${isPlayingPreview ? "playing" : ""}`}
                onClick={() => void handlePreview()}
                disabled={isPlayingPreview || !instruction.trim()}
              >
                <SoundWaveAnimatedIcon size={14} isPlaying={isPlayingPreview} />
                <span>{isPlayingPreview ? "合成播放中..." : "试听音色效果"}</span>
              </button>
            </div>
            <input
              id="voice-preview-text"
              type="text"
              className="voice-design-input voice-preview-input"
              value={previewText}
              maxLength={80}
              onChange={(e) => setPreviewText(e.target.value)}
              placeholder="输入你想让它读的试听句子..."
            />
          </div>

          {errorMsg && (
            <div className="voice-design-error" role="alert">
              ⚠️ {errorMsg}
            </div>
          )}
        </div>

        <div className="voice-design-footer">
          <div className="voice-design-footer-hint">
            <span>✨ 专属音色将保存至本地引擎，可在播报音色中切换</span>
          </div>
          <div className="voice-design-footer-actions">
            <button type="button" className="btn-secondary" onClick={onCancel} disabled={isSubmitting}>
              取消
            </button>
            <button
              type="button"
              className="btn-primary btn-save-voice"
              onClick={() => void handleSubmit()}
              disabled={isSubmitting || !name.trim() || !instruction.trim()}
            >
              {isSubmitting ? "正在固化保存..." : "固化并锁定音色"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
