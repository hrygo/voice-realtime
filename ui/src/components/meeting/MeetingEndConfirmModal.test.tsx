import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MeetingEndConfirmModal } from "./MeetingEndConfirmModal";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

describe("MeetingEndConfirmModal", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("explains the archive consequence and makes continuing the safe default", () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn().mockResolvedValue(true);

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="产品评审会"
          elapsedLabel="42:18"
          segmentCount={18}
          isConfirming={false}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    const dialog = container.querySelector("[role='dialog']");
    expect(dialog).not.toBeNull();
    expect(dialog?.getAttribute("aria-labelledby")).toBe("meeting-end-confirm-title");
    expect(container.textContent).toContain("要现在结束这场会议吗？");
    expect(container.textContent).toContain("产品评审会");
    expect(container.textContent).toContain("42:18");
    expect(container.textContent).toContain("18");
    expect(container.textContent).toContain("冲刷最后一段转录");
    expect(container.textContent).toContain("AI 纪要将在后台排队生成");

    const continueButton = container.querySelector(
      ".meeting-end-confirm-cancel",
    ) as HTMLButtonElement;
    expect(continueButton).not.toBeNull();
    expect(document.activeElement).toBe(continueButton);

    act(() => {
      continueButton.click();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("closes only after the end command confirms successfully", async () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn().mockResolvedValue(true);

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="周会"
          elapsedLabel="08:00"
          segmentCount={4}
          isConfirming={false}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    const endButton = container.querySelector(
      ".meeting-end-confirm-submit",
    ) as HTMLButtonElement;
    await act(async () => {
      endButton.click();
      await Promise.resolve();
    });

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the modal open when the end command cannot be confirmed", async () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn().mockResolvedValue(false);

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="周会"
          elapsedLabel="08:00"
          segmentCount={4}
          isConfirming={false}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    const endButton = container.querySelector(
      ".meeting-end-confirm-submit",
    ) as HTMLButtonElement;
    await act(async () => {
      endButton.click();
      await Promise.resolve();
    });

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(container.querySelector("[role='dialog']")).not.toBeNull();
  });

  it("locks the decision while the end command is still pending", async () => {
    let resolveConfirm!: (value: boolean) => void;
    const onClose = vi.fn();
    const onConfirm = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveConfirm = resolve;
        }),
    );

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="周会"
          elapsedLabel="08:00"
          segmentCount={4}
          isConfirming={false}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    const endButton = container.querySelector(
      ".meeting-end-confirm-submit",
    ) as HTMLButtonElement;
    const continueButton = container.querySelector(
      ".meeting-end-confirm-cancel",
    ) as HTMLButtonElement;
    const closeButton = container.querySelector(
      ".meeting-end-confirm-close",
    ) as HTMLButtonElement;

    await act(async () => {
      endButton.click();
      await Promise.resolve();
    });

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(endButton.disabled).toBe(true);
    expect(continueButton.disabled).toBe(true);
    expect(closeButton.disabled).toBe(true);

    act(() => {
      endButton.click();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      resolveConfirm(true);
      await Promise.resolve();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("supports Escape and disables dismissal while archiving", () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn().mockResolvedValue(true);

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="周会"
          elapsedLabel="08:00"
          segmentCount={4}
          isConfirming={false}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);

    act(() => {
      root.render(
        <MeetingEndConfirmModal
          isOpen={true}
          meetingTitle="周会"
          elapsedLabel="08:00"
          segmentCount={4}
          isConfirming={true}
          onClose={onClose}
          onConfirm={onConfirm}
        />,
      );
    });

    const endButton = container.querySelector(
      ".meeting-end-confirm-submit",
    ) as HTMLButtonElement;
    const closeButton = container.querySelector(
      ".meeting-end-confirm-close",
    ) as HTMLButtonElement;
    expect(endButton.disabled).toBe(true);
    expect(closeButton.disabled).toBe(true);

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
