import { hasUnsafeUrlChars } from "../lib/safeUrl";

/**
 * Куда вернуться после входа: только путь этого сайта. Отклоняются //host, обратный слэш
 * и управляющие символы (браузер вырезает TAB/LF: "/\t/evil" стал бы "//evil"), их
 * %-коды; итог дополнительно разбирается URL-парсером и сверяется с origin (аудит дня 7).
 */
export function safeReturnPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "/";
  if (hasUnsafeUrlChars(value) || /%(5c|09|0a|0d|00)/i.test(value)) return "/";
  let url: URL;
  try {
    url = new URL(value, window.location.origin);
  } catch {
    return "/";
  }
  if (url.origin !== window.location.origin) return "/";
  if (url.pathname.startsWith("/login") || url.pathname.startsWith("/register")) return "/";
  return value; // проверки пройдены — исходная строка (без перекодирования кириллицы)
}
