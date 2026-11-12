import { useMutation, useQueryClient } from "@tanstack/react-query";
import { errorMessage } from "../../api/client";
import type { ReactionKind, VideoCard } from "../../api/types";
import { setReaction, videoKey } from "../../api/videos";
import { useAuth } from "../../auth/AuthContext";
import { useLoginRedirect } from "../../auth/useLoginRedirect";
import { DislikeIcon, LikeIcon } from "../../components/Icons";
import { formatCount } from "../../lib/format";
import styles from "./WatchPage.module.css";

/** Лайк и дизлайк. Повторный клик по своей реакции снимает её. Счётчики — из ответа сервера. */
export function ReactionButtons({ video }: { video: VideoCard }) {
  const { user } = useAuth();
  const toLogin = useLoginRedirect();
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (kind: ReactionKind | null) => setReaction(video.id, kind),
    // Поздний ответ перезапроса карточки не затрёт счётчики из ответа реакции.
    onMutate: () => queryClient.cancelQueries({ queryKey: videoKey(video.id) }),
    onSuccess: (out) => {
      queryClient.setQueryData<VideoCard>(videoKey(video.id), (old) =>
        old
          ? { ...old, my_reaction: out.my_reaction, likes_count: out.likes_count, dislikes_count: out.dislikes_count }
          : old,
      );
    },
  });

  const mine = video.my_reaction ?? null;
  function toggle(kind: ReactionKind) {
    if (!user) return toLogin();
    mutation.mutate(mine === kind ? null : kind);
  }

  return (
    <div className={styles.reactionsWrap}>
      <div className={styles.reactions} role="group" aria-label="Оценка видео">
        <button
          type="button"
          className={styles.reaction}
          aria-pressed={mine === "like"}
          aria-label={`Нравится: ${formatCount(video.likes_count)}`}
          disabled={mutation.isPending || user === undefined}
          onClick={() => toggle("like")}
        >
          <LikeIcon filled={mine === "like"} />
          <span>{formatCount(video.likes_count)}</span>
        </button>
        <button
          type="button"
          className={styles.reaction}
          aria-pressed={mine === "dislike"}
          aria-label={`Не нравится: ${formatCount(video.dislikes_count)}`}
          disabled={mutation.isPending || user === undefined}
          onClick={() => toggle("dislike")}
        >
          <DislikeIcon filled={mine === "dislike"} />
          <span>{formatCount(video.dislikes_count)}</span>
        </button>
      </div>
      {mutation.isError && (
        <span className={styles.inlineError} role="alert">
          {errorMessage(mutation.error)}
        </span>
      )}
    </div>
  );
}
