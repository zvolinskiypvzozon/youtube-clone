import { useRef, useState } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { ApiError, errorMessage } from "../../api/client";
import { banUser } from "../../api/comments";
import {
  dismissReport,
  fetchReports,
  refreshAfterModeration,
  reportsKey,
  setVideoModeration,
  unbanUser,
} from "../../api/moderation";
import type { ReportQueueItem, ReportStatus } from "../../api/types";
import { InfiniteFooter } from "../../components/InfiniteFooter";
import { Spinner } from "../../components/Spinner";
import { StateMessage } from "../../components/StateMessage";
import { UserText } from "../../components/UserText";
import { formatRelative } from "../../lib/format";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { useAuth } from "../../auth/AuthContext";
import { ReasonAction } from "../../components/ReasonAction";
import styles from "./ModerationPage.module.css";

const STATUSES: [ReportStatus, string][] = [
  ["open", "Открытые"],
  ["resolved", "Видео скрыто"],
  ["dismissed", "Отклонённые"],
];

export const CATEGORY: Record<string, string> = {
  spam: "Спам или обман",
  violence: "Насилие или жестокость",
  adult: "Контент для взрослых",
  copyright: "Нарушение авторских прав",
  other: "Другое",
};

const VIDEO_STATUS: Record<string, string> = {
  uploading: "загружается",
  processing: "обрабатывается",
  ready: "готово",
  failed: "ошибка обработки",
};

const VISIBILITY: Record<string, string> = {
  public: "открытый доступ",
  unlisted: "по ссылке",
  private: "ограниченный доступ",
};

export function ModerationPage() {
  useDocumentTitle("Модерация");
  const [params, setParams] = useSearchParams();
  const raw = params.get("status");
  const status: ReportStatus = raw === "resolved" || raw === "dismissed" ? raw : "open";
  const reports = useInfiniteQuery({
    queryKey: reportsKey(status),
    queryFn: ({ pageParam, signal }) => fetchReports(status, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = reports.data?.pages.flatMap((p) => p.items) ?? [];
  // После действия кнопка (а часто и вся карточка) исчезает: фокус — на заголовок, итог —
  // объявлением для скринридера (ревью дня 8).
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [announce, setAnnounce] = useState("");
  const onDone = (message: string) => {
    setAnnounce(message);
    headingRef.current?.focus();
  };

  return (
    <section className={styles.page} aria-labelledby="moderation-title">
      <h1 id="moderation-title" ref={headingRef} tabIndex={-1} className={styles.title}>
        Модерация: жалобы
      </h1>
      <p className="visually-hidden" role="status">
        {announce}
      </p>
      <div className={styles.filters} role="group" aria-label="Статус жалоб">
        {STATUSES.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={styles.filter}
            aria-pressed={status === value}
            onClick={() => setParams(value === "open" ? {} : { status: value }, { replace: true })}
          >
            {label}
          </button>
        ))}
      </div>

      {reports.isPending ? (
        <Spinner />
      ) : reports.isError && !reports.data ? (
        <StateMessage title="Не удалось загрузить жалобы" role="alert" onRetry={() => void reports.refetch()}>
          {errorMessage(reports.error)}
        </StateMessage>
      ) : items.length === 0 ? (
        <StateMessage title={status === "open" ? "Открытых жалоб нет" : "Жалоб нет"} />
      ) : (
        <ul className={styles.list}>
          {items.map((report) => (
            <ReportCard key={report.id} report={report} onDone={onDone} />
          ))}
        </ul>
      )}
      <InfiniteFooter query={reports} />
    </section>
  );
}

function ReportCard({ report, onDone }: { report: ReportQueueItem; onDone: (message: string) => void }) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { video } = report;
  const open = report.status === "open";
  // Бан владельца — не для своего видео (сервер ответит 403: себя банить нельзя; аудит дня 8).
  const ownVideo = Boolean(video.owner_id && video.owner_id === user?.id);

  /**
   * Выполнить действие и обновить очередь. 409/404 — жалобу или видео уже обработал другой
   * модератор: очередь всё равно обновляется (иначе карточка висела бы устаревшей — ревью
   * дня 8), итог объявляется; ошибка остаётся у формы, если карточка ещё на месте.
   */
  async function run(action: () => Promise<unknown>, done: string) {
    try {
      await action();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 404)) {
        await refreshAfterModeration(queryClient);
        onDone(errorMessage(e));
      }
      throw e;
    }
    await refreshAfterModeration(queryClient);
    onDone(done);
  }

  return (
    <li className={styles.card}>
      <div className={styles.cardHead}>
        <span className={styles.category}>{CATEGORY[report.category] ?? report.category}</span>
        <time dateTime={report.created_at} className={styles.muted}>
          {formatRelative(report.created_at)}
        </time>
        <span className={styles.muted}>
          от{" "}
          {report.reporter ? <UserText text={report.reporter.username} /> : "удалённого пользователя"}
        </span>
      </div>

      {report.comment && <UserText text={report.comment} as="p" className={styles.comment} lines={6} />}

      <div className={styles.video}>
        <span className={styles.label}>Видео:</span>{" "}
        {video.id ? (
          <Link to={`/watch/${encodeURIComponent(video.id)}`}>
            <UserText text={video.title} />
          </Link>
        ) : (
          <>
            <UserText text={video.title} /> <span className={styles.badge}>удалено</span>
          </>
        )}{" "}
        <span className={styles.muted}>
          канал <UserText text={`@${video.channel_handle}`} />
        </span>
        <div className={styles.badges}>
          {video.status && <span className={styles.badge}>{VIDEO_STATUS[video.status] ?? video.status}</span>}
          {video.visibility && <span className={styles.badge}>{VISIBILITY[video.visibility] ?? video.visibility}</span>}
          {video.moderation === "hidden" && <span className={`${styles.badge} ${styles.danger}`}>скрыто модерацией</span>}
          {video.owner_banned && <span className={`${styles.badge} ${styles.danger}`}>владелец заблокирован</span>}
        </div>
      </div>

      <div className={styles.actions}>
        {video.id && video.moderation === "ok" && (
          <ReasonAction
            label="Скрыть видео"
            reasonLabel="Причина скрытия (её увидит владелец)"
            submitLabel="Скрыть"
            required
            onSubmit={(reason) => run(() => setVideoModeration(video.id as string, "hidden", reason), "Видео скрыто")}
          />
        )}
        {video.id && video.moderation === "hidden" && (
          <ReasonAction
            label="Вернуть видео"
            reasonLabel="Вернуть видео в доступ? Закрытые жалобы не откроются."
            submitLabel="Вернуть"
            required={false}
            withReason={false}
            onSubmit={() => run(() => setVideoModeration(video.id as string, "ok"), "Видео возвращено")}
          />
        )}
        {open && (
          <ReasonAction
            label="Отклонить жалобу"
            reasonLabel="Причина отклонения"
            submitLabel="Отклонить"
            required={false}
            onSubmit={(reason) => run(() => dismissReport(report.id, reason || undefined), "Жалоба отклонена")}
          />
        )}
        {video.owner_id && video.owner_banned === false && !ownVideo && (
          <ReasonAction
            label="Заблокировать владельца"
            reasonLabel="Причина блокировки"
            submitLabel="Заблокировать"
            required
            onSubmit={(reason) => run(() => banUser(video.owner_id as string, reason), "Владелец заблокирован")}
          />
        )}
        {video.owner_id && video.owner_banned === true && !ownVideo && (
          <ReasonAction
            label="Разблокировать владельца"
            reasonLabel="Снять блокировку? Контент владельца снова станет виден."
            submitLabel="Разблокировать"
            required={false}
            withReason={false}
            onSubmit={() => run(() => unbanUser(video.owner_id as string), "Владелец разблокирован")}
          />
        )}
      </div>
    </li>
  );
}
