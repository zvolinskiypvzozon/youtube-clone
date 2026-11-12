import { Link } from "react-router-dom";
import { StateMessage } from "../components/StateMessage";
import { useDocumentTitle } from "../lib/useDocumentTitle";

export function NotFoundPage() {
  useDocumentTitle("Страница не найдена");
  return (
    <StateMessage title="Страница не найдена">
      Проверьте адрес или вернитесь <Link to="/">на главную</Link>.
    </StateMessage>
  );
}
