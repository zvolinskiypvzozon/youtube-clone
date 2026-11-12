import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import * as authApi from "../api/auth";
import { ApiError } from "../api/client";
import { ME_KEY, resetSessionData } from "../api/queryClient";
import type { LoginIn, Me, RegisterIn } from "../api/types";

interface AuthState {
  /** undefined — ещё не известно (загрузка или ошибка), null — аноним. */
  user: Me | null | undefined;
  /** Не удалось узнать, кто вошёл (сеть, 5xx): показать ошибку и «Повторить». */
  meError: unknown;
  /** Аноним после выхода в этой вкладке: закрытые страницы ведут на главную, не на вход. */
  loggedOut: boolean;
  retryMe: () => void;
  login: (data: LoginIn) => Promise<Me>;
  register: (data: RegisterIn) => Promise<Me>;
  /** Выход: после успеха — на главную. Ошибка (кроме 401) пробрасывается, сессия остаётся. */
  logout: () => Promise<void>;
  /** Удаление аккаунта: после успеха — как выход (сброс кэша, главная, аноним). Ошибка
   * (неверный пароль, последний админ, сеть) пробрасывается, аккаунт и сессия остаются. */
  deleteAccount: (password: string) => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [loggedOut, setLoggedOut] = useState(false);
  const me = useQuery({
    queryKey: ME_KEY,
    queryFn: ({ signal }) => authApi.fetchMe(signal),
    staleTime: 5 * 60_000,
    // Выход или вход в другой вкладке — заметим при возврате в эту (аудит дня 7).
    refetchOnWindowFocus: "always",
  });

  // Смена пользователя, которую заметили не вход/выход этой вкладки: 401 на любом запросе
  // или /me при возврате во вкладку (вход или выход в другой вкладке) — тот же сброс данных
  // прежней сессии (аудит дня 8: иначе следующему пользователю остались бы поля staff,
  // can_delete и черновики). Смену, сделанную switchUser, не сбрасываем второй раз.
  const lastUser = useRef<string | null | undefined>(undefined);
  const handledUser = useRef<string | null | undefined>(undefined);
  const currentUser = me.data === undefined ? undefined : (me.data?.id ?? null);
  useEffect(() => {
    if (currentUser === undefined) return;
    const previous = lastUser.current;
    lastUser.current = currentUser;
    if (handledUser.current === currentUser) {
      handledUser.current = undefined;
      return;
    }
    if (previous !== undefined && previous !== currentUser) void resetSessionData(queryClient);
  }, [currentUser, queryClient]);

  // Смена пользователя: запрос /me, начатый под прежней сессией, отменяется (иначе его
  // поздний ответ затёр бы нового пользователя), остальные данные сбрасываются и активные
  // запросы перезапрашиваются уже под новой сессией.
  const switchUser = useCallback(
    async (user: Me | null) => {
      handledUser.current = user?.id ?? null;
      await queryClient.cancelQueries({ queryKey: ME_KEY });
      queryClient.setQueryData(ME_KEY, user);
      await resetSessionData(queryClient);
    },
    [queryClient],
  );

  const { refetch } = me;
  const value = useMemo<AuthState>(
    () => ({
      user: me.data,
      meError: me.data === undefined && me.isError ? me.error : null,
      loggedOut,
      retryMe: () => void refetch(),
      login: async (data) => {
        const user = await authApi.login(data);
        setLoggedOut(false);
        await switchUser(user);
        return user;
      },
      register: async (data) => {
        const user = await authApi.register(data);
        setLoggedOut(false);
        await switchUser(user);
        return user;
      },
      logout: async () => {
        try {
          await authApi.logout();
        } catch (error) {
          // 401 — сессии уже нет: это тоже выход. Иначе (CSRF, сеть, 5xx) сессия жива —
          // не показываем «вышли».
          if (!(error instanceof ApiError && error.status === 401)) throw error;
        }
        // После выхода — на главную. Переход React Router v7 идёт в startTransition
        // (низкий приоритет), поэтому закрытая страница сама ведёт на главную (loggedOut),
        // а не на вход.
        setLoggedOut(true);
        navigate("/");
        await switchUser(null);
      },
      deleteAccount: async (password) => {
        await authApi.deleteAccount(password);
        // Сессия и cookie удалены сервером: тот же путь, что после выхода (решение дня 9).
        setLoggedOut(true);
        navigate("/");
        await switchUser(null);
      },
    }),
    [me.data, me.isError, me.error, loggedOut, refetch, switchUser, navigate],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth вне AuthProvider");
  return ctx;
}
