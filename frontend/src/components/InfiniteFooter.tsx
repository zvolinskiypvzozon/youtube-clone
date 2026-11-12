import type { UseInfiniteQueryResult } from "@tanstack/react-query";
import { errorMessage } from "../api/client";
import { Spinner } from "./Spinner";
import { StateMessage } from "./StateMessage";

/**
 * Подвал списка с курсором: «Показать ещё», спиннер подгрузки, ошибка с «Повторить».
 * Подгрузка — только по клику: ошибка не зацикливает запросы (ревью дня 7).
 */
export function InfiniteFooter({ query }: { query: UseInfiniteQueryResult<unknown, unknown> }) {
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage, error } = query;
  return (
    <>
      {isFetchNextPageError && (
        <StateMessage title="Не удалось загрузить продолжение" role="alert" onRetry={() => void fetchNextPage()}>
          {errorMessage(error)}
        </StateMessage>
      )}
      {isFetchingNextPage ? (
        <Spinner />
      ) : (
        hasNextPage &&
        !isFetchNextPageError && (
          <div>
            <button type="button" className="button button-secondary" onClick={() => void fetchNextPage()}>
              Показать ещё
            </button>
          </div>
        )
      )}
    </>
  );
}
