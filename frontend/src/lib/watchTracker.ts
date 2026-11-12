// Учёт фактического воспроизведения для засчитывания просмотра (решение архитектора дня 8):
// порог — min(30 с, половина длительности); перемотка и время на паузе не считаются.

export const VIEW_THRESHOLD_MAX = 30;
/** Шаг timeupdate больше этого — не воспроизведение, а скачок (перемотка, пробуждение вкладки). */
const MAX_STEP = 1.5;

export function viewThreshold(duration: number | null | undefined): number {
  if (!duration || !Number.isFinite(duration) || duration <= 0) return VIEW_THRESHOLD_MAX;
  return Math.min(VIEW_THRESHOLD_MAX, duration / 2);
}

export interface WatchTracker {
  /** Новая позиция воспроизведения; true — порог достигнут впервые (засчитать просмотр). */
  update(currentTime: number, threshold: number): boolean;
  /** Перемотка: следующий шаг не суммируется. */
  seek(): void;
  readonly played: number;
}

export function createWatchTracker(): WatchTracker {
  let last: number | null = null;
  let played = 0;
  let done = false;
  return {
    update(currentTime, threshold) {
      if (last !== null) {
        const step = currentTime - last;
        if (step > 0 && step <= MAX_STEP) played += step;
      }
      last = currentTime;
      if (done || played < threshold) return false;
      done = true;
      return true;
    },
    seek() {
      last = null;
    },
    get played() {
      return played;
    },
  };
}
