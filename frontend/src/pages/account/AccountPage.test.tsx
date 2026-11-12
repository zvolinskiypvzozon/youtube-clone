import { fireEvent, screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAuth } from "../../auth/RequireAuth";
import { Layout } from "../../components/Layout";
import { json, ME, mockApi, renderApp, type MockRoutes } from "../../test/utils";
import { AccountPage } from "./AccountPage";

function render() {
  return renderApp(
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<p>главная</p>} />
        <Route
          path="account"
          element={
            <RequireAuth>
              <AccountPage />
            </RequireAuth>
          }
        />
      </Route>
    </Routes>,
    { route: "/account" },
  );
}

function api(routes: MockRoutes = {}) {
  let deleted = false;
  const del = vi.fn((_u: URL, init: RequestInit) => {
    expect(JSON.parse(String(init.body))).toEqual({ password: "secret-1" });
    deleted = true;
    return new Response(null, { status: 204 });
  });
  const fetchMock = mockApi({
    "GET /api/auth/me": () => (deleted ? json({ detail: "Требуется вход" }, 401) : ME),
    "DELETE /api/me": del,
    ...routes,
  });
  return { del, fetchMock };
}

afterEach(() => vi.unstubAllGlobals());

describe("AccountPage: удаление аккаунта", () => {
  it("с паролем — DELETE /api/me, затем главная анонимом", async () => {
    const { del } = api();
    render();
    fireEvent.change(await screen.findByLabelText("Пароль для подтверждения"), { target: { value: "secret-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Удалить аккаунт навсегда" }));
    expect(await screen.findByText("главная")).toBeInTheDocument();
    expect(del).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
    // Интерфейс анонима: в шапке — «Войти».
    expect(await screen.findByRole("link", { name: "Войти" })).toBeInTheDocument();
  });

  it("без пароля — ошибка у поля, запроса нет", async () => {
    const { del } = api();
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Удалить аккаунт навсегда" }));
    expect(screen.getByText("Введите пароль")).toBeInTheDocument();
    expect(del).not.toHaveBeenCalled();
  });

  it("неверный пароль (403) — «Неверный пароль», остаёмся на странице", async () => {
    api({ "DELETE /api/me": () => json({ detail: "Forbidden" }, 403) });
    render();
    fireEvent.change(await screen.findByLabelText("Пароль для подтверждения"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Удалить аккаунт навсегда" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Неверный пароль");
    expect(screen.getByTestId("location")).toHaveTextContent("/account");
    expect(screen.getByLabelText("Пароль для подтверждения")).toHaveAttribute("aria-describedby");
  });

  it("последний админ (409) — сообщение сервера", async () => {
    api({ "DELETE /api/me": () => json({ detail: "Нельзя удалить последнего администратора" }, 409) });
    render();
    fireEvent.change(await screen.findByLabelText("Пароль для подтверждения"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Удалить аккаунт навсегда" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Нельзя удалить последнего администратора");
    await waitFor(() => expect(screen.getByRole("button", { name: "Удалить аккаунт навсегда" })).toBeEnabled());
  });

  it("ник и email выводятся текстом", async () => {
    api({ "GET /api/auth/me": () => ({ ...ME, username: "alice", email: "<b>x</b>@example.com" }) });
    const { container } = render();
    expect(await screen.findByText("<b>x</b>@example.com")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("main b")).toBeNull();
  });
});
