import React, { useState } from "react";
import { showToast } from "../Toast";
import { copyTextToClipboard } from "../../utils/clipboard";

interface MarkdownRendererProps {
  content: string;
}

function CodeBlock({ language, code }: { language: string; code: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await copyTextToClipboard(code);
      setCopied(true);
      showToast("代码已复制到剪贴板", "success");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      showToast("复制代码失败", "error");
    }
  };

  return (
    <div
      className="markdown-code-block"
      style={{
        background: "rgba(15, 23, 42, 0.75)",
        border: "1px solid rgba(255, 255, 255, 0.1)",
        borderRadius: "8px",
        overflow: "hidden",
        margin: "8px 0",
      }}
    >
      <div
        className="markdown-code-header"
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "4px 10px",
          background: "rgba(255, 255, 255, 0.04)",
          borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
          fontSize: "0.72rem",
          color: "var(--text-muted)",
        }}
      >
        <span style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
          {(language || "CODE").toUpperCase()}
        </span>
        <button
          type="button"
          className="btn-copy-code"
          onClick={() => void handleCopy()}
          style={{
            background: "transparent",
            border: "none",
            color: copied ? "var(--color-green)" : "var(--text-secondary)",
            cursor: "pointer",
            fontSize: "0.72rem",
            padding: "2px 6px",
            borderRadius: "4px",
            display: "inline-flex",
            alignItems: "center",
            gap: "4px",
          }}
          title="复制代码"
        >
          {copied ? "✓ 已复制" : "📋 复制"}
        </button>
      </div>
      <pre
        style={{
          margin: 0,
          padding: "10px 12px",
          overflowX: "auto",
          fontFamily: "var(--font-mono, monospace)",
          fontSize: "0.82rem",
          lineHeight: 1.5,
          color: "#e2e8f0",
        }}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  if (!content) return <p style={{ color: "var(--text-muted)" }}>暂无 Markdown 内容</p>;

  const parseInline = (text: string): React.ReactNode[] => {
    // Split by inline code `...` and bold **...**
    const tokens = text.split(/(\*\*.*?\*\*|`.*?`)/g);
    return tokens.map((token, i) => {
      if (token.startsWith("**") && token.endsWith("**") && token.length >= 4) {
        return <strong key={i} style={{ color: "var(--text-primary)" }}>{token.slice(2, -2)}</strong>;
      }
      if (token.startsWith("`") && token.endsWith("`") && token.length >= 2) {
        return (
          <code
            key={i}
            style={{
              background: "var(--bg-tertiary)",
              padding: "1px 5px",
              borderRadius: "4px",
              fontFamily: "var(--font-mono)",
              fontSize: "0.8em",
              color: "var(--color-accent-light)",
            }}
          >
            {token.slice(1, -1)}
          </code>
        );
      }
      return token;
    });
  };

  // Pre-process fenced code blocks
  const lines = content.split("\n");
  const elements: React.ReactNode[] = [];
  let inCodeBlock = false;
  let codeLanguage = "";
  let codeBuffer: string[] = [];

  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];
    const trimmed = line.trim();

    if (trimmed.startsWith("```")) {
      if (inCodeBlock) {
        // End code block
        elements.push(
          <CodeBlock
            key={`code-${idx}`}
            language={codeLanguage}
            code={codeBuffer.join("\n")}
          />,
        );
        inCodeBlock = false;
        codeLanguage = "";
        codeBuffer = [];
      } else {
        // Start code block
        inCodeBlock = true;
        codeLanguage = trimmed.slice(3).trim();
        codeBuffer = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeBuffer.push(line);
      continue;
    }

    if (!trimmed) {
      elements.push(<div key={`space-${idx}`} style={{ height: "4px" }} />);
      continue;
    }

    if (trimmed.startsWith("### ")) {
      elements.push(
        <h4
          key={`h4-${idx}`}
          style={{
            fontSize: "0.92rem",
            fontWeight: 700,
            color: "var(--color-accent-light)",
            margin: "8px 0 2px 0",
          }}
        >
          {parseInline(trimmed.slice(4))}
        </h4>,
      );
      continue;
    }

    if (trimmed.startsWith("## ")) {
      elements.push(
        <h3
          key={`h3-${idx}`}
          style={{
            fontSize: "1.05rem",
            fontWeight: 700,
            color: "var(--text-primary)",
            borderBottom: "1px solid var(--border-dim)",
            paddingBottom: "4px",
            margin: "12px 0 4px 0",
          }}
        >
          {parseInline(trimmed.slice(3))}
        </h3>,
      );
      continue;
    }

    if (trimmed.startsWith("# ")) {
      elements.push(
        <h2
          key={`h2-${idx}`}
          style={{
            fontSize: "1.2rem",
            fontWeight: 800,
            color: "var(--text-primary)",
            margin: "6px 0",
          }}
        >
          {parseInline(trimmed.slice(2))}
        </h2>,
      );
      continue;
    }

    if (trimmed.startsWith("---") || trimmed.startsWith("***")) {
      elements.push(
        <hr
          key={`hr-${idx}`}
          style={{
            border: "none",
            borderTop: "1px solid var(--border)",
            margin: "8px 0",
          }}
        />,
      );
      continue;
    }

    if (trimmed.startsWith("> ")) {
      elements.push(
        <blockquote
          key={`quote-${idx}`}
          style={{
            borderLeft: "3px solid var(--color-accent)",
            paddingLeft: "12px",
            margin: "4px 0",
            color: "var(--text-secondary)",
            fontStyle: "italic",
          }}
        >
          {parseInline(trimmed.slice(2))}
        </blockquote>,
      );
      continue;
    }

    if (trimmed.startsWith("- [ ] ")) {
      elements.push(
        <div key={`todo-${idx}`} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
          <span>☐</span>
          <span>{parseInline(trimmed.slice(6))}</span>
        </div>,
      );
      continue;
    }

    if (trimmed.startsWith("- [x] ") || trimmed.startsWith("- [X] ")) {
      elements.push(
        <div key={`done-${idx}`} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
          <span style={{ color: "var(--color-green)" }}>☑</span>
          <span>{parseInline(trimmed.slice(6))}</span>
        </div>,
      );
      continue;
    }

    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      elements.push(
        <div key={`list-${idx}`} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
          <span style={{ color: "var(--color-accent)" }}>•</span>
          <span>{parseInline(trimmed.slice(2))}</span>
        </div>,
      );
      continue;
    }

    elements.push(
      <p key={`p-${idx}`} style={{ margin: 0 }}>
        {parseInline(trimmed)}
      </p>,
    );
  }

  // Handle unclosed code block if streaming
  if (inCodeBlock && codeBuffer.length > 0) {
    elements.push(
      <CodeBlock
        key="code-unclosed"
        language={codeLanguage}
        code={codeBuffer.join("\n")}
      />,
    );
  }

  return (
    <div
      className="markdown-rich-container"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        fontSize: "0.86rem",
        lineHeight: 1.6,
        color: "var(--text-primary)",
      }}
    >
      {elements}
    </div>
  );
}
