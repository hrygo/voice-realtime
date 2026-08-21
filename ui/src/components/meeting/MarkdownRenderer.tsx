import React from "react";

interface MarkdownRendererProps {
  content: string;
}

export function MarkdownRenderer({ content }: MarkdownRendererProps) {
  if (!content) return <p style={{ color: "var(--text-muted)" }}>暂无 Markdown 内容</p>;

  const lines = content.split("\n");

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
      {lines.map((line, idx) => {
        const trimmed = line.trim();

        if (!trimmed) {
          return <div key={idx} style={{ height: "4px" }} />;
        }

        if (trimmed.startsWith("### ")) {
          return (
            <h4
              key={idx}
              style={{
                fontSize: "0.92rem",
                fontWeight: 700,
                color: "var(--color-accent-light)",
                margin: "8px 0 2px 0",
              }}
            >
              {parseInline(trimmed.slice(4))}
            </h4>
          );
        }

        if (trimmed.startsWith("## ")) {
          return (
            <h3
              key={idx}
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
            </h3>
          );
        }

        if (trimmed.startsWith("# ")) {
          return (
            <h2
              key={idx}
              style={{
                fontSize: "1.2rem",
                fontWeight: 800,
                color: "var(--text-primary)",
                margin: "6px 0",
              }}
            >
              {parseInline(trimmed.slice(2))}
            </h2>
          );
        }

        if (trimmed.startsWith("---") || trimmed.startsWith("***")) {
          return (
            <hr
              key={idx}
              style={{
                border: "none",
                borderTop: "1px solid var(--border)",
                margin: "8px 0",
              }}
            />
          );
        }

        if (trimmed.startsWith("> ")) {
          return (
            <blockquote
              key={idx}
              style={{
                borderLeft: "3px solid var(--color-accent)",
                paddingLeft: "12px",
                margin: "4px 0",
                color: "var(--text-secondary)",
                fontStyle: "italic",
              }}
            >
              {parseInline(trimmed.slice(2))}
            </blockquote>
          );
        }

        if (trimmed.startsWith("- [ ] ")) {
          return (
            <div key={idx} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
              <span>☐</span>
              <span>{parseInline(trimmed.slice(6))}</span>
            </div>
          );
        }

        if (trimmed.startsWith("- [x] ") || trimmed.startsWith("- [X] ")) {
          return (
            <div key={idx} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
              <span style={{ color: "var(--color-green)" }}>☑</span>
              <span>{parseInline(trimmed.slice(6))}</span>
            </div>
          );
        }

        if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
          return (
            <div key={idx} style={{ display: "flex", alignItems: "baseline", gap: "6px" }}>
              <span style={{ color: "var(--color-accent)" }}>•</span>
              <span>{parseInline(trimmed.slice(2))}</span>
            </div>
          );
        }

        return <p key={idx} style={{ margin: 0 }}>{parseInline(trimmed)}</p>;
      })}
    </div>
  );
}
