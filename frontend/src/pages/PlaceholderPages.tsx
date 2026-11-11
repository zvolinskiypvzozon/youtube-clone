import { Link } from "react-router-dom";
import { StateMessage } from "../components/StateMessage";
import { useDocumentTitle } from "../lib/useDocumentTitle";

/** Экраны дня 8 (просмотр, канал, загрузка, «Мои видео») — пока заглушка. */
export function ComingSoonPage({ title }: { title: string }) {
  useDocumentTitle(title);
  return (
    <StateMessage title={title}>
      Этот раздел появится в следующем обновлении. <Link to="/">На главную</Link>
    </StateMessage>
  );
}

export function NotFoundPage() {
  useDocumentTitle("Страница не найдена");
  return (
    <StateMessage title="Страница не найдена">
      Проверьте адрес или вернитесь <Link to="/">на главную</Link>.
    </StateMessage>
  );
}
