import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/** Действие требует входа: на страницу входа с возвратом сюда (адрес — в состоянии навигации). */
export function useLoginRedirect(): () => void {
  const navigate = useNavigate();
  const location = useLocation();
  return useCallback(
    () => navigate("/login", { state: { from: location.pathname + location.search } }),
    [navigate, location.pathname, location.search],
  );
}
