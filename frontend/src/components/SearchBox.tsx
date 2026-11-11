import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { SEARCH_MAX } from "../api/videos";
import { SEARCH_DEBOUNCE_MS, useDebouncedValue } from "../lib/useDebouncedValue";
import { ClearIcon, SearchIcon } from "./Icons";
import styles from "./SearchBox.module.css";

/**
 * Поиск в шапке. На главной результаты обновляются по паузе в наборе (debounce 300 мс,
 * решение 2026-11-10: поток запросов упирается в зону search), запрос — в URL (?q=):
 * первый запрос — новая запись истории, уточнения её заменяют; «назад» возвращает ленту.
 * На других страницах — по Enter (переход на главную с ?q=).
 */
export function searchPath(q: string): string {
  return q ? `/?q=${encodeURIComponent(q)}` : "/";
}

export function SearchBox() {
  const [params] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const onHome = location.pathname === "/";
  const urlQuery = onHome ? (params.get("q") ?? "") : "";

  const [value, setValue] = useState(urlQuery);
  const debounced = useDebouncedValue(value, SEARCH_DEBOUNCE_MS);

  // URL сменился извне (назад/вперёд, ссылка) — поле показывает его. Подстройка при рендере,
  // а не в эффекте (рекомендация React для состояния, производного от входных данных).
  // Ключ — страница и запрос: уход на главную по логотипу тоже очищает поле.
  const urlKey = `${location.pathname}?${urlQuery}`;
  const [seenUrlKey, setSeenUrlKey] = useState(urlKey);
  if (urlKey !== seenUrlKey) {
    const leftHome = !onHome && seenUrlKey.startsWith("/?");
    setSeenUrlKey(urlKey);
    // С главной на другую страницу поле не трогаем (запрос можно уточнить и нажать Enter).
    if (!leftHome && value.trim() !== urlQuery) setValue(urlQuery);
  }

  // Поиск — только по новому значению после паузы, не при смене URL извне (иначе «назад»
  // вернул бы набранный ранее запрос).
  const appliedRef = useRef(debounced);
  useEffect(() => {
    if (debounced === appliedRef.current) return;
    appliedRef.current = debounced;
    const q = debounced.trim();
    // Первый запрос — новая запись истории («назад» вернёт ленту), уточнение — замена
    // (история не засоряется промежуточными вариантами).
    if (onHome && q !== urlQuery) navigate(searchPath(q), { replace: urlQuery !== "" });
  }, [debounced, onHome, urlQuery, navigate]);

  function go(q: string, replace: boolean) {
    navigate(searchPath(q), { replace });
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    go(value.trim(), false);
  }

  function clear() {
    setValue("");
    if (onHome) go("", false);
  }

  return (
    <form className={styles.form} role="search" onSubmit={submit}>
      <div className={styles.field}>
        <input
          className={styles.input}
          type="search"
          name="q"
          placeholder="Поиск"
          aria-label="Поиск по названию видео"
          maxLength={SEARCH_MAX}
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        {value && (
          <button type="button" className={styles.clear} onClick={clear} aria-label="Очистить запрос">
            <ClearIcon />
          </button>
        )}
      </div>
      <button type="submit" className={styles.submit} aria-label="Найти">
        <SearchIcon />
      </button>
    </form>
  );
}
