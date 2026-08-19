import { useState, useEffect } from "react";
import "./Toast.css";

export type ToastType = "success" | "info" | "warning" | "error";

export interface ToastItem {
  id: string;
  message: string;
  type: ToastType;
  duration?: number;
}

type ToastListener = (toasts: ToastItem[]) => void;

let toastList: ToastItem[] = [];
const listeners = new Set<ToastListener>();

function notify() {
  listeners.forEach((l) => l([...toastList]));
}

export const showToast = (message: string, type: ToastType = "info", duration = 3000) => {
  const id = Math.random().toString(36).substring(2, 9);
  const newToast: ToastItem = { id, message, type, duration };
  toastList = [...toastList, newToast];
  notify();

  if (duration > 0) {
    setTimeout(() => {
      removeToast(id);
    }, duration);
  }
};

export const removeToast = (id: string) => {
  toastList = toastList.filter((t) => t.id !== id);
  notify();
};

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastItem[]>(toastList);

  useEffect(() => {
    const listener: ToastListener = (updated) => setToasts(updated);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container" aria-live="polite" aria-atomic="true">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast-item toast-${toast.type}`}
          role="status"
          onClick={() => removeToast(toast.id)}
        >
          <span className="toast-icon">
            {toast.type === "success" && "✓"}
            {toast.type === "info" && "ℹ"}
            {toast.type === "warning" && "⚠"}
            {toast.type === "error" && "✕"}
          </span>
          <span className="toast-message">{toast.message}</span>
        </div>
      ))}
    </div>
  );
}
