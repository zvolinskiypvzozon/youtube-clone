import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Route, Routes } from "react-router-dom";
import { json, ME, mockApi, renderApp } from "../test/utils";
import { LoginPage } from "./LoginPage";
import { RegisterPage } from "./RegisterPage";

const anonymous = () => new Response(null, { status: 401 });

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function routes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="*" element={<p>другая страница</p>} />
    </Routes>
  );
}

describe("вход", () => {
  it("успех → возврат на исходную страницу, логин без пробелов", async () => {
    let body: unknown;
    mockApi({
      "GET /api/auth/me": anonymous,
      "POST /api/auth/login": (_, init) => {
        body = JSON.parse(String(init.body));
        return ME;
      },
    });
    renderApp(routes(), { route: "/login", state: { from: "/upload" } });
    fill("Email или имя пользователя", "  alice ");
    fill("Пароль", "secret-123");
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/upload"));
    expect(body).toEqual({ login: "alice", password: "secret-123" });
  });

  it("внешний адрес возврата не используется (open redirect)", async () => {
    mockApi({ "GET /api/auth/me": anonymous, "POST /api/auth/login": () => ME });
    renderApp(routes(), { route: "/login", state: { from: "//evil.example/x" } });
    fill("Email или имя пользователя", "alice");
    fill("Пароль", "secret-123");
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/));
  });

  it("401 — сообщение сервера; пустые поля — проверка до запроса", async () => {
    const fetchMock = mockApi({
      "GET /api/auth/me": anonymous,
      "POST /api/auth/login": () => json({ detail: "Неверный логин или пароль" }, 401),
    });
    renderApp(routes(), { route: "/login" });
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByText("Введите пароль.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/auth/login")).toBe(false);

    fill("Email или имя пользователя", "alice");
    fill("Пароль", "wrong-pass");
    fireEvent.click(screen.getByRole("button", { name: "Войти" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Неверный логин или пароль");
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
  });

  it("вошедшего пользователя страница входа отправляет дальше", async () => {
    mockApi({ "GET /api/auth/me": () => ME });
    renderApp(routes(), { route: "/login" });
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/));
  });
});

describe("регистрация", () => {
  it("проверка полей на клиенте, по-русски", async () => {
    const fetchMock = mockApi({ "GET /api/auth/me": anonymous });
    renderApp(routes(), { route: "/register" });
    fill("Email", "not-an-email");
    fill("Имя пользователя", "ал");
    fill("Пароль", "short");
    fill("Повторите пароль", "other");
    fireEvent.click(screen.getByRole("button", { name: "Создать аккаунт" }));
    expect(await screen.findByText("Введите корректный email.")).toBeInTheDocument();
    expect(screen.getByText(/Имя: 3–30 символов/)).toBeInTheDocument();
    expect(screen.getByText(/Пароль: от 8/)).toBeInTheDocument();
    expect(screen.getByText("Пароли не совпадают.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u) === "/api/auth/register")).toBe(false);
  });

  it("409 — сообщение сервера; 422 — поле подсвечено русским текстом", async () => {
    let status = 409;
    mockApi({
      "GET /api/auth/me": anonymous,
      "POST /api/auth/register": () =>
        status === 409
          ? json({ detail: "Имя пользователя уже занято" }, 409)
          : json({ detail: "x", errors: [{ loc: ["body", "username"], msg: "reserved" }] }, 422),
    });
    renderApp(routes(), { route: "/register" });
    fill("Email", "bob@example.com");
    fill("Имя пользователя", "admin");
    fill("Пароль", "long-enough-1");
    fill("Повторите пароль", "long-enough-1");
    fireEvent.click(screen.getByRole("button", { name: "Создать аккаунт" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Имя пользователя уже занято");

    status = 422;
    fireEvent.click(screen.getByRole("button", { name: "Создать аккаунт" }));
    expect(await screen.findByText(/Имя: 3–30 символов/)).toBeInTheDocument();
    expect(screen.getByLabelText("Имя пользователя")).toHaveAttribute("aria-invalid", "true");
  });
});
