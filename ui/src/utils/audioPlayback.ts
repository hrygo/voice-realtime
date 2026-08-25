/** 播放 Blob 音频，并保证所有终态都释放临时 object URL。 */
export function playAudioBlob(blob: Blob): Promise<void> {
  const audioUrl = URL.createObjectURL(blob);
  const audio = new Audio(audioUrl);

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      audio.onended = null;
      audio.onerror = null;
      URL.revokeObjectURL(audioUrl);
      if (error) reject(error);
      else resolve();
    };

    audio.onended = () => finish();
    audio.onerror = () => finish(new Error("audio playback failed"));
    audio.play().catch((error: unknown) => {
      finish(error instanceof Error ? error : new Error("audio playback was rejected"));
    });
  });
}
