import type { VideoCard } from "../../api/types";
import { UserText } from "../../components/UserText";
import styles from "./WatchPage.module.css";

const VISIBILITY: Record<string, string> = {
  public: "Открытый доступ",
  unlisted: "Доступ по ссылке",
  private: "Ограниченный доступ",
};

/** Служебное состояние видео — приходит только владельцу и staff (state != null). */
export function VideoStateBanner({ state }: { state: NonNullable<VideoCard["state"]> }) {
  return (
    <div className={styles.stateBox} role="status">
      <span className={styles.badge}>{VISIBILITY[state.visibility] ?? state.visibility}</span>
      {(state.status === "uploading" || state.status === "processing") && (
        <p>Видео обрабатывается. Страница обновится, когда оно будет готово.</p>
      )}
      {state.status === "failed" && (
        <p>
          Не удалось обработать видео
          {state.failure_reason && (
            <>
              : <UserText text={state.failure_reason} />
            </>
          )}
        </p>
      )}
      {state.moderation === "hidden" && (
        <p className={styles.danger}>
          Видео скрыто модерацией
          {state.moderation_reason && (
            <>
              . Причина: <UserText text={state.moderation_reason} className={styles.preWrap} />
            </>
          )}
        </p>
      )}
    </div>
  );
}
