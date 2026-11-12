/**
 * URL из данных API для href/src: только путь этого сайта (/yt-media/…, /watch/…), не
 * //host, не /\host, не схема (javascript:, data:, https:). Иначе undefined — атрибут не
 * ставится. ESLint пропускает в href/src/action/poster только вызов safeUrl.
 */
export function safeUrl(value: string | null | undefined): string | undefined {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return undefined;
  if (hasUnsafeUrlChars(value)) return undefined;
  return value;
}

/** Управляющие символы (браузер вырезает TAB/LF из URL) и обратный слэш (= «/» в URL). */
export function hasUnsafeUrlChars(value: string): boolean {
  for (const ch of value) {
    const code = ch.charCodeAt(0);
    if (code < 0x20 || code === 0x7f || ch === "\\") return true;
  }
  return false;
}

/**
 * Для присваивания DOM-свойству (video.src = …): ESLint пропускает в src/href/poster только
 * safeUrl/requireSafeUrl. Значение уже проверено safeUrl — небезопасное здесь означает ошибку
 * в коде, поэтому исключение, а не тихая подстановка.
 */
export function requireSafeUrl(value: string): string {
  const url = safeUrl(value);
  if (url === undefined) throw new Error("Небезопасный URL");
  return url;
}
