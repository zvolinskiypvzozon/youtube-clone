import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError, errorMessage, NetworkError, type FieldErrors } from "../../api/client";
import { myVideosKey } from "../../api/myVideos";
import type { VideoVisibility } from "../../api/types";
import { cancelUpload, DESCRIPTION_MAX, MAX_UPLOAD_BYTES, startUpload, TITLE_MAX } from "../../api/uploads";
import { finishUpload } from "../../lib/finishUpload";
import { uploadErrorMessage, uploadParts } from "../../lib/uploadParts";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import styles from "./UploadPage.module.css";

export const VISIBILITY_OPTIONS: [VideoVisibility, string, string][] = [
  ["public", "Открытый доступ", "Видео видят все, оно есть в ленте и поиске"],
  ["unlisted", "Доступ по ссылке", "Видео открывается по ссылке, в ленте и поиске его нет"],
  ["private", "Ограниченный доступ", "Только вы и те, кому вы откроете доступ"],
];

type Phase =
  | { kind: "form" }
  | { kind: "uploading"; loaded: number; total: number }
  | { kind: "completing" }
  | { kind: "done"; videoId: string }
  | { kind: "cancelled" };

/** Проверка файла до запроса к API: видео, не пустой, не больше 2 ГБ (окончательно — сервер и ffprobe). */
export function validateFile(file: File | null): string | null {
  if (!file) return "Выберите файл";
  if (!/^video\//i.test(file.type)) return "Это не видеофайл. Выберите видео (MP4, WebM, MOV и т. п.)";
  if (file.size === 0) return "Файл пустой";
  if (file.size > MAX_UPLOAD_BYTES) return "Файл больше 2 ГБ";
  return null;
}

/** Название по умолчанию — имя файла без расширения. */
export function titleFromFile(name: string): string {
  return name.replace(/\.[^.]+$/, "").trim().slice(0, TITLE_MAX);
}

function formatMb(bytes: number): string {
  return `${(bytes / 1024 / 1024).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;
}

export function UploadPage() {
  useDocumentTitle("Загрузка видео");
  const queryClient = useQueryClient();
  const ids = { file: useId(), title: useId(), description: useId() };
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<VideoVisibility>("public");
  const [phase, setPhase] = useState<Phase>({ kind: "form" });
  const [fields, setFields] = useState<FieldErrors>({});
  const [error, setError] = useState<string | null>(null);
  // Объявление для скринридера — по вехам, а не на каждое событие прогресса (ревью дня 8).
  const [announce, setAnnounce] = useState("");
  // Название, подставленное из имени файла: при смене файла заменяется, пока его не правили.
  const [autoTitle, setAutoTitle] = useState("");

  // Текущая загрузка: отмена (кнопка, уход со страницы) прерывает отправку частей и удаляет
  // незавершённую загрузку на сервере — иначе она занимала бы место в лимите «3 в работе».
  const active = useRef<{ controller: AbortController } | null>(null);

  const busy = phase.kind === "uploading" || phase.kind === "completing";

  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  // Уход со страницы во время отправки частей — та же отмена: submit поймает AbortError и
  // удалит незавершённую загрузку (в том числе если ответ на старт придёт уже после ухода).
  // Когда все части отправлены (completing), отменить уже нельзя: видео уйдёт в обработку.
  useEffect(() => () => active.current?.controller.abort(), []);

  function chooseFile(next: File | null) {
    setFile(next);
    setFields({});
    setError(null);
    if (next && (!title.trim() || title === autoTitle)) {
      const fromFile = titleFromFile(next.name);
      setAutoTitle(fromFile);
      setTitle(fromFile);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const fileError = validateFile(file);
    const errors: FieldErrors = {};
    if (fileError) errors.file = fileError;
    if (!title.trim()) errors.title = "Введите название";
    setFields(errors);
    if (Object.keys(errors).length || !file) return;

    const controller = new AbortController();
    active.current = { controller };
    setPhase({ kind: "uploading", loaded: 0, total: file.size });
    setAnnounce("Загрузка началась");
    let videoId: string | null = null;
    let lastPercent = 0;
    try {
      const started = await startUpload({
        title,
        description,
        visibility,
        content_type: file.type.toLowerCase(),
        size_bytes: file.size,
      });
      videoId = started.video_id;
      if (controller.signal.aborted) throw new DOMException("Загрузка отменена", "AbortError");
      const parts = await uploadParts(file, started.parts, {
        signal: controller.signal,
        onProgress: (loaded, total) => {
          if (active.current?.controller !== controller) return;
          // Перерисовка — только при смене целого процента (событий XHR — десятки в секунду).
          const percent = Math.floor((loaded / total) * 100);
          if (percent === lastPercent && loaded < total) return;
          if (Math.floor(percent / 25) > Math.floor(lastPercent / 25)) setAnnounce(`Загружено ${percent}%`);
          lastPercent = percent;
          setPhase({ kind: "uploading", loaded, total });
        },
      });
      // «Отменить» успели нажать, пока последняя часть завершалась.
      if (controller.signal.aborted) throw new DOMException("Загрузка отменена", "AbortError");
      setPhase({ kind: "completing" });
      setAnnounce("Завершаем загрузку");
      await finishUpload(videoId, parts);
      active.current = null;
      void queryClient.invalidateQueries({ queryKey: myVideosKey });
      setPhase({ kind: "done", videoId });
    } catch (e) {
      const cancelled = e instanceof DOMException && e.name === "AbortError";
      // Незавершённую загрузку — удалить (лимит «3 в работе»); ошибку удаления не показываем:
      // брошенные загрузки сервер чистит сам.
      if (videoId) cancelUpload(videoId).catch(() => undefined);
      active.current = null;
      if (cancelled) {
        setAnnounce("");
        setPhase({ kind: "cancelled" });
        return;
      }
      setPhase({ kind: "form" });
      if (e instanceof ApiError && e.status === 422) {
        // Поля формы — у полей; тип и размер файла — у поля «Файл».
        const file = e.fields.content_type ?? e.fields.size_bytes;
        setFields({ title: e.fields.title ?? "", description: e.fields.description ?? "", ...(file ? { file } : {}) });
        setError(e.fields.title || e.fields.description || file ? null : e.message);
      } else if (e instanceof ApiError || e instanceof NetworkError) {
        setError(errorMessage(e));
      } else {
        setError(uploadErrorMessage(e));
      }
    }
  }

  function cancel() {
    active.current?.controller.abort();
  }

  function reset() {
    setFile(null);
    setTitle("");
    setDescription("");
    setVisibility("public");
    setFields({});
    setError(null);
    setPhase({ kind: "form" });
    setAutoTitle("");
  }

  if (phase.kind === "done") {
    return (
      <div className={styles.page}>
        <div className={styles.card} role="status">
          <h1 className={styles.title}>Видео загружено</h1>
          <p>Оно обрабатывается — это займёт от нескольких секунд до нескольких минут.</p>
          <div className={styles.actions}>
            <Link to={`/watch/${encodeURIComponent(phase.videoId)}`} className="button button-primary">
              Открыть видео
            </Link>
            <Link to="/my/videos" className="button button-secondary">
              Мои видео
            </Link>
            <button type="button" className="button button-secondary" onClick={reset}>
              Загрузить ещё
            </button>
          </div>
        </div>
      </div>
    );
  }

  const percent = phase.kind === "uploading" && phase.total > 0 ? Math.floor((phase.loaded / phase.total) * 100) : 0;

  return (
    <div className={styles.page}>
      <form className={styles.card} onSubmit={submit} noValidate aria-label="Загрузка видео">
        <h1 className={styles.title}>Загрузка видео</h1>
        <p className="visually-hidden" role="status">
          {announce}
        </p>
        {phase.kind === "cancelled" && (
          <p className={styles.notice} role="status">
            Загрузка отменена.
          </p>
        )}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}

        <fieldset className={styles.fieldset} disabled={busy}>
          <div className={styles.field}>
            <label htmlFor={ids.file} className={styles.label}>
              Файл
            </label>
            <input
              id={ids.file}
              type="file"
              accept="video/*"
              aria-invalid={fields.file ? true : undefined}
              aria-describedby={`${ids.file}-hint`}
              onChange={(e) => chooseFile(e.target.files?.[0] ?? null)}
            />
            <span id={`${ids.file}-hint`} className={fields.file ? styles.fieldError : styles.hint}>
              {fields.file ?? (file ? `${formatMb(file.size)}` : "Видео до 2 ГБ и до 60 минут")}
            </span>
          </div>

          <div className={styles.field}>
            <label htmlFor={ids.title} className={styles.label}>
              Название
            </label>
            <input
              id={ids.title}
              dir="auto"
              className={styles.input}
              maxLength={TITLE_MAX}
              value={title}
              aria-invalid={fields.title ? true : undefined}
              aria-describedby={fields.title ? `${ids.title}-error` : undefined}
              onChange={(e) => setTitle(e.target.value)}
            />
            {fields.title ? (
              <span id={`${ids.title}-error`} className={styles.fieldError}>
                {fields.title}
              </span>
            ) : (
              title && title === autoTitle && <span className={styles.hint}>Название взято из имени файла — его увидят зрители</span>
            )}
          </div>

          <div className={styles.field}>
            <label htmlFor={ids.description} className={styles.label}>
              Описание
            </label>
            <textarea
              id={ids.description}
              dir="auto"
              className={styles.textarea}
              rows={4}
              maxLength={DESCRIPTION_MAX}
              value={description}
              aria-invalid={fields.description ? true : undefined}
              aria-describedby={fields.description ? `${ids.description}-error` : undefined}
              onChange={(e) => setDescription(e.target.value)}
            />
            {fields.description && (
              <span id={`${ids.description}-error`} className={styles.fieldError}>
                {fields.description}
              </span>
            )}
          </div>

          <fieldset className={styles.radios}>
            <legend className={styles.label}>Доступ</legend>
            {VISIBILITY_OPTIONS.map(([value, label, hint]) => (
              <label key={value} className={styles.radio}>
                <input
                  type="radio"
                  name="visibility"
                  value={value}
                  checked={visibility === value}
                  onChange={() => setVisibility(value)}
                />
                <span>
                  {label}
                  <span className={styles.hint}> — {hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
        </fieldset>

        {busy ? (
          <div className={styles.progressBox}>
            <progress
              className={styles.progress}
              max={100}
              value={phase.kind === "completing" ? 100 : percent}
              aria-label="Прогресс загрузки"
            />
            <span>
              {phase.kind === "completing"
                ? "Завершаем загрузку…"
                : `Загружено ${percent}% (${formatMb(phase.loaded)} из ${formatMb(phase.total)})`}
            </span>
            <button
              type="button"
              className="button button-secondary"
              onClick={cancel}
              disabled={phase.kind === "completing"}
            >
              Отменить
            </button>
          </div>
        ) : (
          <button type="submit" className="button button-primary">
            Загрузить
          </button>
        )}
      </form>
    </div>
  );
}
