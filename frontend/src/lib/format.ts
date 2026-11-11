// Форматирование для интерфейса на русском.

const compact = new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 });
const plural = new Intl.PluralRules("ru-RU");
const relative = new Intl.RelativeTimeFormat("ru-RU", { numeric: "auto" });

const VIEW_FORMS: Record<string, string> = {
  one: "просмотр",
  few: "просмотра",
  many: "просмотров",
  other: "просмотра",
};

/** «1 просмотр», «3 просмотра», «25 просмотров», «1,2 тыс. просмотров». */
export function formatViews(count: number): string {
  if (count >= 1000) return `${compact.format(count)} просмотров`;
  return `${count} ${VIEW_FORMS[plural.select(count)]}`;
}

/** 0:05, 12:34, 1:02:03. */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

/** «только что», «5 минут назад», «вчера», «3 недели назад». */
export function formatRelative(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((new Date(iso).getTime() - now.getTime()) / 1000);
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size) return relative.format(Math.trunc(seconds / size), unit);
  }
  return "только что";
}
