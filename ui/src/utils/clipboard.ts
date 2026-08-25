/**
 * Copy text from a user-initiated action with a compatibility fallback.
 *
 * Clipboard.writeText is unavailable outside secure contexts and can reject
 * when the browser denies the permission. The fallback keeps local/dev builds
 * usable while still reporting failure when neither path succeeds.
 */
export async function copyTextToClipboard(text: string): Promise<void> {
  try {
    const clipboard = navigator.clipboard;
    if (clipboard && typeof clipboard.writeText === "function") {
      await clipboard.writeText(text);
      return;
    }
  } catch {
    // Try the legacy path below before reporting a failure to the caller.
  }

  if (typeof document.execCommand !== "function" || !document.body) {
    throw new Error("Clipboard write failed");
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  textarea.dataset.clipboardFallback = "true";
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";

  document.body.appendChild(textarea);
  try {
    textarea.select();
    if (!document.execCommand("copy")) {
      throw new Error("Clipboard write failed");
    }
  } finally {
    textarea.remove();
  }
}
