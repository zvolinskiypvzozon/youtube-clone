import { focusManager, useQuery } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { api } from "../api/typed";
import { ME, mockApi, renderApp } from "../test/utils";
import { useAuth } from "./AuthContext";

/** Данные, зависящие от того, кто вошёл (как будущие «Мои видео»). */
function Private() {
  const { user, login } = useAuth();
  // Любой запрос, зависящий от сессии; тело в тесте — { owner } (тип ответа здесь не важен).
  const data = useQuery({
    queryKey: ["private"],
    queryFn: () => api("get", "/api/me/videos") as unknown as Promise<{ owner: string }>,
  });
  return (
    <div>
      <p>владелец: {data.data?.owner ?? "—"}</p>
      <p>вошёл: {user ? user.username : "нет"}</p>
      <button type="button" onClick={() => void login({ login: "bob", password: "x".repeat(8) })}>
        войти как bob
      </button>
      <button type="button" onClick={() => void data.refetch()}>
        обновить
      </button>
    </div>
  );
}

describe("смена пользователя", () => {
  it.each([
    ["другой пользователь", { ...ME, id: "u2", username: "bob" }, "bob"],
    ["аноним", null, "аноним"],
  ])("/me при возврате во вкладку: %s — данные прежней сессии сброшены (аудит дня 8)", async (_c, next, owner) => {
    let me: object | null = ME;
    let session = "alice";
    mockApi({
      "GET /api/auth/me": () => (me ? me : new Response(null, { status: 401 })),
      "GET /api/me/videos": () => ({ owner: session }),
    });
    renderApp(<Private />);
    expect(await screen.findByText("владелец: alice")).toBeInTheDocument();
    // Вход или выход в другой вкладке: сервер уже отвечает за другого пользователя.
    me = next;
    session = owner;
    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      expect(await screen.findByText(`владелец: ${owner}`)).toBeInTheDocument();
    } finally {
      focusManager.setFocused(undefined);
    }
  });

  it("/me при возврате — тот же пользователь: данные не сбрасываются", async () => {
    let calls = 0;
    mockApi({
      "GET /api/auth/me": () => ME,
      "GET /api/me/videos": () => ({ owner: `alice-${++calls}` }),
    });
    renderApp(<Private />);
    expect(await screen.findByText("владелец: alice-1")).toBeInTheDocument();
    try {
      act(() => {
        focusManager.setFocused(false);
        focusManager.setFocused(true);
      });
      await act(async () => undefined);
      await waitFor(() => expect(calls).toBe(1));
      expect(screen.getByText("владелец: alice-1")).toBeInTheDocument();
    } finally {
      focusManager.setFocused(undefined);
    }
  });

  it("вход сбрасывает данные прежней сессии и запрашивает их заново", async () => {
    let session = "аноним";
    mockApi({
      "GET /api/auth/me": () => new Response(null, { status: 401 }),
      "POST /api/auth/login": () => {
        session = "bob";
        return { ...ME, username: "bob" };
      },
      "GET /api/me/videos": () => ({ owner: session }),
    });
    renderApp(<Private />);
    expect(await screen.findByText("владелец: аноним")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "войти как bob" }));
    expect(await screen.findByText("вошёл: bob")).toBeInTheDocument();
    expect(await screen.findByText("владелец: bob")).toBeInTheDocument();
  });

  it("поздний ответ /me, начатый до входа, не затирает вошедшего пользователя", async () => {
    let releaseMe: (r: Response) => void = () => {};
    mockApi({
      "GET /api/auth/me": () => new Promise<Response>((resolve) => (releaseMe = resolve)),
      "POST /api/auth/login": () => ({ ...ME, username: "bob" }),
      "GET /api/me/videos": () => ({ owner: "bob" }),
    });
    renderApp(<Private />);
    await waitFor(() => expect(screen.getByText("вошёл: нет")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "войти как bob" }));
    expect(await screen.findByText("вошёл: bob")).toBeInTheDocument();
    // Отпускаем старый ответ и даём ему примениться (сразу после act он ещё в пути —
    // без паузы тест не ловил отсутствие отмены, мутационная проверка).
    await act(async () => {
      releaseMe(new Response(null, { status: 401 }));
      await new Promise((r) => setTimeout(r, 100));
    });
    expect(screen.getByText("вошёл: bob")).toBeInTheDocument();
  });

  it("401 у вошедшего (сессия истекла): аноним, данные сессии сброшены, без цикла запросов", async () => {
    let expired = false;
    const fetchMock = mockApi({
      "GET /api/auth/me": () => (expired ? new Response(null, { status: 401 }) : ME),
      "GET /api/me/videos": () => (expired ? new Response(null, { status: 401 }) : { owner: "alice" }),
    });
    const client = renderApp(<Private />);
    expect(await screen.findByText("владелец: alice")).toBeInTheDocument();
    expired = true;
    // Любой запрос получил 401 — например, перезапрос данных.
    fireEvent.click(screen.getByRole("button", { name: "обновить" }));
    expect(await screen.findByText("вошёл: нет")).toBeInTheDocument();
    expect(screen.getByText("владелец: —")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 300));
    const privateCalls = fetchMock.mock.calls.filter(([u]) => String(u) === "/api/me/videos").length;
    expect(privateCalls).toBeLessThanOrEqual(3); // начальный, ручной, один после сброса
    client.unmount();
  });
});

