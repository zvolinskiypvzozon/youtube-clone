import { useMutation, useQueryClient } from "@tanstack/react-query";
import { errorMessage } from "../api/client";
import { channelKey, setSubscription } from "../api/channels";
import type { Channel } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { useLoginRedirect } from "../auth/useLoginRedirect";
import styles from "./SubscribeButton.module.css";

/** Подписка на канал. Своему каналу кнопки нет; аноним уходит на вход. Счётчик — из ответа сервера. */
export function SubscribeButton({ channel }: { channel: Channel }) {
  const { user } = useAuth();
  const toLogin = useLoginRedirect();
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (subscribe: boolean) => setSubscription(channel.handle, subscribe),
    onMutate: () => queryClient.cancelQueries({ queryKey: channelKey(channel.handle) }),
    onSuccess: (out) => {
      queryClient.setQueryData<Channel>(channelKey(channel.handle), (old) =>
        old ? { ...old, subscribed: out.subscribed, subscribers_count: out.subscribers_count } : old,
      );
    },
  });

  if (user && user.channel.id === channel.id) return null;
  const subscribed = Boolean(channel.subscribed);

  return (
    <div className={styles.wrap}>
      <button
        type="button"
        className={`button ${subscribed ? "button-secondary" : "button-primary"}`}
        aria-pressed={subscribed}
        disabled={mutation.isPending || user === undefined}
        onClick={() => (user ? mutation.mutate(!subscribed) : toLogin())}
      >
        {subscribed ? "Вы подписаны" : "Подписаться"}
      </button>
      {mutation.isError && (
        <span className={styles.error} role="alert">
          {errorMessage(mutation.error)}
        </span>
      )}
    </div>
  );
}
