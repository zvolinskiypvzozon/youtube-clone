import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { errorMessage } from "../api/client";
import { Spinner } from "../components/Spinner";
import { StateMessage } from "../components/StateMessage";
import { useAuth } from "./AuthContext";

/** Страница только для вошедших: аноним уходит на вход с возвратом сюда. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, meError, loggedOut, retryMe } = useAuth();
  const location = useLocation();
  if (meError) {
    return (
      <StateMessage title="Не удалось проверить вход" role="alert" onRetry={retryMe}>
        {errorMessage(meError)}
      </StateMessage>
    );
  }
  if (user === undefined) return <Spinner />;
  if (user === null) {
    if (loggedOut) return <Navigate to="/" replace />;
    const from = location.pathname + location.search;
    return <Navigate to="/login" replace state={{ from }} />;
  }
  return <>{children}</>;
}
