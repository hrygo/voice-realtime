import { afterEach, describe, expect, it, vi } from "vitest";
import { copyTextToClipboard } from "./clipboard";

describe("copyTextToClipboard", () => {
  const originalClipboard = navigator.clipboard;
  const originalExecCommand = document.execCommand;

  afterEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: originalExecCommand,
    });
    document.querySelectorAll("textarea[data-clipboard-fallback]").forEach((element) => {
      element.remove();
    });
    vi.restoreAllMocks();
  });

  it("uses the modern Clipboard API when it is available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    await expect(copyTextToClipboard("字幕内容")).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("字幕内容");
  });

  it("falls back to a temporary textarea when the Clipboard API is unavailable", async () => {
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    await expect(copyTextToClipboard("回退复制内容")).resolves.toBeUndefined();

    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea[data-clipboard-fallback]")).toBeNull();
  });

  it("falls back when the Clipboard API rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException("Not allowed", "NotAllowedError"));
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    await expect(copyTextToClipboard("权限失败后的内容")).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("权限失败后的内容");
    expect(execCommand).toHaveBeenCalledWith("copy");
  });

  it("rejects when neither clipboard path succeeds", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("Clipboard unavailable"));
    const execCommand = vi.fn().mockReturnValue(false);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    });

    await expect(copyTextToClipboard("无法复制的内容")).rejects.toThrow(
      "Clipboard write failed",
    );
  });
});
