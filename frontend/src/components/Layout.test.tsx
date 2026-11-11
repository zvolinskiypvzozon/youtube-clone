import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CSRF_HEADER, request } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { RequireAuth } from "../auth/RequireAuth";
import { json, ME, mockApi, renderApp } from "../test/utils";
import { Layout } from "./Layout";

/** Запрос, который отвечает 401, — только после того, как известен вошедший пользователь. */
function ExpiredProbe() {
  const { user } = useAuth();
  const probe = useQuery({ queryKey: ["probe"], queryFn: () => request("/api/probe"), enabled: !!user });
  return <p>{probe.isError ? "ошибка" : "ждём"}</p>;
}

function app() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<p>главная</p>} />
        <Route path="login" element={<p>страница входа</p>} />
        <Route path="upload" element={<RequireAuth><p>загрузка</p></RequireAuth>} />
      </Route>
    </Routes>
  );
}

describe("Layout и сессия", () => {
  it("аноним: кнопка «Войти», закрытая страница уводит на вход", async () => {
    mockApi({ "GET /api/auth/me": () => new Response(null, { status: 401 }) });
    renderApp(app(), { route: "/upload" });
    expect(await screen.findByText("страница входа")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Войти" })).toBeInTheDocument();
    expect(screen.queryByText("Загрузить")).toBeNull();
  });

  it("выход: POST с CSRF, снова аноним, с закрытой страницы — на главную", async () => {
    document.cookie = "csrf=abc; path=/";
    let loggedOut = false;
    const fetchMock = mockApi({
      "GET /api/auth/me": () => (loggedOut ? new Response(null, { status: 401 }) : ME),
      "POST /api/auth/logout": () => {
        loggedOut = true;
        return new Response(null, { status: 204 });
      },
    });
    renderApp(app(), { route: "/upload" });
    fireEvent.click(await screen.findByRole("button", { name: "Меню пользователя alice" }));
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
    expect(await screen.findByRole("link", { name: "Войти" })).toBeInTheDocument();
    // С закрытой страницы — на главную, а не на вход.
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/));
    const logout = fetchMock.mock.calls.find(([u]) => String(u) === "/api/auth/logout");
    expect((logout?.[1]?.headers as Record<string, string>)[CSRF_HEADER]).toBe("abc");
  });

  it("401 на любом запросе (сессия истекла) → интерфейс анонима", async () => {
    mockApi({
      "GET /api/auth/me": () => ME,
      "GET /api/probe": () => new Response(null, { status: 401 }),
    });
    renderApp(
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<ExpiredProbe />} />
        </Route>
      </Routes>,
    );
    await waitFor(() => expect(screen.getByText("ошибка")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Войти" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Меню пользователя alice" })).toBeNull();
  });

  it("ошибка выхода (503): остаёмся вошедшими, причина видна", async () => {
    mockApi({
      "GET /api/auth/me": () => ME,
      "POST /api/auth/logout": () => json({ detail: "x" }, 503),
    });
    renderApp(app(), { route: "/upload" });
    fireEvent.click(await screen.findByRole("button", { name: "Меню пользователя alice" }));
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Не удалось выйти/);
    expect(screen.getByRole("button", { name: "Меню пользователя alice" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Войти" })).toBeNull();
    expect(screen.getByTestId("location")).toHaveTextContent("/upload");
  });

  it("выход при уже истёкшей сессии (401) — тоже выход", async () => {
    mockApi({
      "GET /api/auth/me": () => ME,
      "POST /api/auth/logout": () => json({ detail: "Требуется вход" }, 401),
    });
    renderApp(app(), { route: "/" });
    fireEvent.click(await screen.findByRole("button", { name: "Меню пользователя alice" }));
    fireEvent.click(screen.getByRole("button", { name: "Выйти" }));
    expect(await screen.findByRole("link", { name: "Войти" })).toBeInTheDocument();
  });

  it("Escape закрывает меню и возвращает фокус на кнопку", async () => {
    mockApi({ "GET /api/auth/me": () => ME });
    renderApp(app(), { route: "/" });
    const toggle = await screen.findByRole("button", { name: "Меню пользователя alice" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
  });

  it("/me недоступен (5xx): закрытая страница — ошибка с «Повторить», не вечный спиннер", async () => {
    let down = true;
    mockApi({ "GET /api/auth/me": () => (down ? json({ detail: "x" }, 500) : ME) });
    renderApp(app(), { route: "/upload" });
    expect(await screen.findByText("Не удалось проверить вход")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Войти" })).toBeInTheDocument();
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText("загрузка")).toBeInTheDocument();
  });
});
