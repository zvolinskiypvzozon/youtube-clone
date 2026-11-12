import type { ReactNode } from "react";
import { errorMessage } from "../api/client";
import { Spinner } from "../components/Spinner";
import { StateMessage } from "../components/StateMessage";
import { NotFoundPage } from "../pages/PlaceholderPages";
import { useAuth } from "./AuthContext";

export const STAFF_ROLES = new Set(["moderator", "admin"]);

/**
 * Страница только для модераторов и админов. Это интерфейс: права на каждое действие
 * проверяет API (403). Всем остальным, включая анонима, — «Страница не найдена», как на
 * несуществующем адресе (аудит дня 8). Маршрут всё равно есть в бандле, и /api/admin/*
 * отвечает 401/403 — это не секрет, а однородность (принятый риск в security.md).
 */
export function RequireStaff({ children }: { children: ReactNode }) {
  const { user, meError, retryMe } = useAuth();
  if (meError) {
    return (
      <StateMessage title="Не удалось проверить вход" role="alert" onRetry={retryMe}>
        {errorMessage(meError)}
      </StateMessage>
    );
  }
  if (user === undefined) return <Spinner />;
  if (!user || !STAFF_ROLES.has(user.role)) return <NotFoundPage />;
  return <>{children}</>;
}
