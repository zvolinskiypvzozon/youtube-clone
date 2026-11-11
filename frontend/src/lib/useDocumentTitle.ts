import { useEffect } from "react";

const SUFFIX = "Видеохостинг";

export function useDocumentTitle(title: string | null): void {
  useEffect(() => {
    document.title = title ? `${title} — ${SUFFIX}` : SUFFIX;
  }, [title]);
}
