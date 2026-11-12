import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json, ME, mockApi, renderApp, type Handler } from "../../test/utils";
import { WatchPage } from "./WatchPage";

const BOB = { id: "c2", handle: "bob", title: "Канал Боба" };

function card(overrides: Record<string, unknown> = {}) {
  return {
    id: "v1",
    title: "Видео",
    description: "",
    channel: BOB,
    duration_seconds: 20,
    views_count: 1,
    likes_count: 0,
    dislikes_count: 0,
    comments_count: 2,
    published_at: "2026-11-10T12:00:00Z",
    thumbnail_url: null,
    my_reaction: null,
    state: null,
    ...overrides,
  };
}

function comment(n: number, overrides: Record<string, unknown> = {}) {
  return {
    id: `k${n}`,
    body: `Комментарий ${n}`,
    created_at: "2026-11-11T12:00:00Z",
    author: { id: `ch${n}`, handle: `user${n}`, title: `Автор ${n}` },
    can_delete: false,
    author_user_id: null,
    author_banned: null,
    ...overrides,
  };
}

const STAFF = { ...ME, id: "staff1", role: "moderator", channel: { id: "cs", handle: "mod", title: "Модератор" } };
const anon: Handler = () => json({ detail: "Требуется вход" }, 401);

function api(routes: Record<string, Handler> = {}, { user = ME as object | null } = {}) {
  return mockApi({
    "GET /api/auth/me": user ? () => user : anon,
    "GET /api/videos/v1": () => card(),
    "GET /api/videos/v1/stream": () => ({ video_id: "v1", sources: [], thumbnail_url: null, expires_in: 900 }),
    "GET /api/channels/bob": () => ({ ...BOB, description: "", subscribers_count: 0, created_at: "2026-11-01T00:00:00Z", subscribed: false }),
    "GET /api/videos/v1/comments": () => ({ items: [comment(1), comment(2)], next_cursor: null }),
    ...routes,
  });
}

function render() {
  return renderApp(
    <Routes>
      <Route path="/watch/:videoId" element={<WatchPage />} />
      <Route path="/login" element={<p>Страница входа</p>} />
    </Routes>,
    { route: "/watch/v1" },
  );
}

const list = () => screen.getByRole("list");

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Comments: вывод только текстом", () => {
  it("HTML, Markdown и ссылки в комментарии — текст, без разметки и автоссылок", async () => {
    const body = '<img src=x onerror="alert(1)"> <script>alert(2)</script> **жирный** https://evil.example/';
    api({ "GET /api/videos/v1/comments": () => ({ items: [comment(1, { body })], next_cursor: null }) });
    render();
    const text = await screen.findByText(body);
    expect(text).toHaveAttribute("dir", "auto");
    const section = screen.getByRole("region", { name: /комментари/ });
    expect(section.querySelector("img, script, strong, b")).toBeNull();
    // Единственные ссылки в комментарии — на канал автора (аватар и имя).
    within(list()).getAllByRole("link", { hidden: true }).forEach((a) => expect(a.getAttribute("href")).toBe("/channel/user1"));
  });

  it("RTL-текст изолирован, переносы строк сохраняются", async () => {
    const body = "שלום\nвторая строка";
    api({ "GET /api/videos/v1/comments": () => ({ items: [comment(1, { body, author: { id: "x", handle: "x", title: "אב" } })], next_cursor: null }) });
    render();
    const text = await screen.findByText((_, el) => el?.tagName === "P" && el.textContent === body);
    expect(text).toHaveAttribute("dir", "auto");
    expect(screen.getByText("אב")).toHaveAttribute("dir", "auto");
  });
});

describe("Comments: список", () => {
  it("заголовок со счётчиком, «Показать ещё» передаёт курсор", async () => {
    const get = vi.fn((url: URL) =>
      url.searchParams.get("cursor") === "c2"
        ? { items: [comment(3)], next_cursor: null }
        : { items: [comment(1), comment(2)], next_cursor: "c2" },
    );
    api({ "GET /api/videos/v1/comments": get });
    render();
    expect(await screen.findByRole("heading", { name: "2 комментария" })).toBeInTheDocument();
    await screen.findByText("Комментарий 2");
    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Комментарий 3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
  });

  it("пустой список — «Комментариев пока нет»", async () => {
    api({ "GET /api/videos/v1/comments": () => ({ items: [], next_cursor: null }) });
    render();
    expect(await screen.findByText("Комментариев пока нет.")).toBeInTheDocument();
  });
});

describe("Comments: добавление", () => {
  it("POST с текстом; новый — сверху, счётчик — с сервера, поле очищено", async () => {
    let count = 2;
    const post = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ body: "Привет\nмир" });
      count = 3;
      return json(comment(9, { body: "Привет\nмир", can_delete: true, author: ME.channel }), 201);
    });
    api({ "POST /api/videos/v1/comments": post, "GET /api/videos/v1": () => card({ comments_count: count }) });
    render();
    await screen.findByText("Комментарий 1");
    const field = screen.getByLabelText("Комментарий");
    fireEvent.change(field, { target: { value: "Привет\nмир" } });
    fireEvent.click(screen.getByRole("button", { name: "Комментировать" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(within(list()).getAllByRole("listitem")[0]).toHaveTextContent("Привет"));
    expect(await screen.findByRole("heading", { name: "3 комментария" })).toBeInTheDocument();
    expect(field).toHaveValue("");
  });

  it("пустой или из пробелов — кнопка неактивна", async () => {
    api();
    render();
    await screen.findByText("Комментарий 1");
    const button = screen.getByRole("button", { name: "Комментировать" });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Комментарий"), { target: { value: "   \n " } });
    expect(button).toBeDisabled();
  });

  it("422 — сообщение сервера по полю, текст не теряется", async () => {
    api({
      "POST /api/videos/v1/comments": () =>
        json({ detail: "Ошибка проверки", errors: [{ loc: ["body", "body"], msg: "Текст без видимых символов" }] }, 422),
    });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.change(screen.getByLabelText("Комментарий"), { target: { value: "​ x" } });
    fireEvent.click(screen.getByRole("button", { name: "Комментировать" }));
    expect(await screen.findByText("Текст без видимых символов")).toBeInTheDocument();
    expect(screen.getByLabelText("Комментарий")).toHaveValue("​ x");
  });

  it("429 — понятное сообщение", async () => {
    api({ "POST /api/videos/v1/comments": () => json({ detail: "Too Many" }, 429) });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.change(screen.getByLabelText("Комментарий"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Комментировать" }));
    expect(await screen.findByText(/Слишком много запросов/)).toBeInTheDocument();
  });

  it("аноним — без формы, кнопка «Войдите» ведёт на вход", async () => {
    api({}, { user: null });
    render();
    await screen.findByText("Комментарий 1");
    expect(screen.queryByLabelText("Комментарий")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Войдите" }));
    expect(await screen.findByText("Страница входа")).toBeInTheDocument();
  });

  it("скрытое модерацией видео — формы нет, комментарии видны", async () => {
    api({
      "GET /api/videos/v1": () =>
        card({ state: { status: "ready", visibility: "public", moderation: "hidden", moderation_reason: "x", failure_reason: null } }),
    });
    render();
    await screen.findByText("Комментарий 1");
    expect(screen.queryByLabelText("Комментарий")).not.toBeInTheDocument();
    expect(screen.getByText(/Новые комментарии закрыты/)).toBeInTheDocument();
  });
});

describe("Comments: удаление", () => {
  it("кнопка «Удалить» — только при can_delete", async () => {
    api({ "GET /api/videos/v1/comments": () => ({ items: [comment(1, { can_delete: true }), comment(2)], next_cursor: null }) });
    render();
    const items = await waitFor(() => within(list()).getAllByRole("listitem"));
    expect(within(items[0]).getByRole("button", { name: "Удалить" })).toBeInTheDocument();
    expect(within(items[1]).queryByRole("button", { name: "Удалить" })).not.toBeInTheDocument();
  });

  it("подтверждение → DELETE, комментарий убран, счётчик — с сервера", async () => {
    let count = 2;
    const del = vi.fn(() => {
      count = 1;
      return new Response(null, { status: 204 });
    });
    api({
      "GET /api/videos/v1": () => card({ comments_count: count }),
      "GET /api/videos/v1/comments": () => ({ items: [comment(1, { can_delete: true }), comment(2)], next_cursor: null }),
      "DELETE /api/comments/k1": del,
    });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    expect(del).not.toHaveBeenCalled();
    fireEvent.click(within(list()).getAllByRole("button", { name: "Удалить" })[0]);
    await waitFor(() => expect(screen.queryByText("Комментарий 1")).not.toBeInTheDocument());
    expect(del).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("heading", { name: "1 комментарий" })).toBeInTheDocument();
    // Фокус — на заголовок секции (кнопка исчезла), удаление объявлено.
    expect(screen.getByRole("heading", { name: "1 комментарий" })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("Комментарий удалён");
  });

  it("403 — сообщение, комментарий остаётся", async () => {
    api({
      "GET /api/videos/v1/comments": () => ({ items: [comment(1, { can_delete: true })], next_cursor: null }),
      "DELETE /api/comments/k1": () => json({ detail: "Нет прав" }, 403),
    });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    fireEvent.click(within(list()).getAllByRole("button", { name: "Удалить" })[0]);
    expect(await screen.findByText("Нет прав")).toBeInTheDocument();
    expect(screen.getByText("Комментарий 1")).toBeInTheDocument();
  });
});

describe("Comments: staff", () => {
  it("поля staff null — ни пометки, ни кнопки бана", async () => {
    api({}, { user: STAFF });
    render();
    await screen.findByText("Комментарий 1");
    expect(screen.queryByText("Автор заблокирован")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Заблокировать автора" })).not.toBeInTheDocument();
  });

  it("пометка «Автор заблокирован» и кнопка бана — по полям от сервера", async () => {
    api(
      {
        "GET /api/videos/v1/comments": () => ({
          items: [
            comment(1, { author_user_id: "u7", author_banned: true }),
            comment(2, { author_user_id: "u8", author_banned: false }),
            comment(3, { author_user_id: STAFF.id, author_banned: false }),
          ],
          next_cursor: null,
        }),
      },
      { user: STAFF },
    );
    render();
    const items = await waitFor(() => within(list()).getAllByRole("listitem"));
    expect(within(items[0]).getByText("Автор заблокирован")).toBeInTheDocument();
    expect(within(items[0]).queryByRole("button", { name: "Заблокировать автора" })).not.toBeInTheDocument();
    expect(within(items[1]).getByRole("button", { name: "Заблокировать автора" })).toBeInTheDocument();
    // Себя — нет (сервер ответил бы 403).
    expect(within(items[2]).queryByRole("button", { name: "Заблокировать автора" })).not.toBeInTheDocument();
  });

  it("бан: причина обязательна, POST с причиной, список перезапрашивается", async () => {
    let banned = false;
    const ban = vi.fn((_u: URL, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toEqual({ reason: "спам" });
      banned = true;
      return { user_id: "u8", username: "user2", role: "user", is_banned: true, banned_reason: "спам", banned_by_role: "moderator" };
    });
    api(
      {
        "GET /api/videos/v1/comments": () => ({ items: [comment(2, { author_user_id: "u8", author_banned: banned })], next_cursor: null }),
        "POST /api/admin/users/u8/ban": ban,
      },
      { user: STAFF },
    );
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Заблокировать автора" }));
    const submit = screen.getByRole("button", { name: "Заблокировать" });
    expect(submit).toBeDisabled();
    expect(screen.getByLabelText(/Причина блокировки/)).toHaveFocus();
    fireEvent.change(screen.getByLabelText(/Причина блокировки/), { target: { value: "спам" } });
    fireEvent.click(submit);
    expect(await within(list()).findByText("Автор заблокирован")).toBeInTheDocument();
    expect(ban).toHaveBeenCalledTimes(1);
  });

  it("бан: 403 от сервера показывается текстом", async () => {
    api(
      {
        "GET /api/videos/v1/comments": () => ({ items: [comment(2, { author_user_id: "u8", author_banned: false })], next_cursor: null }),
        "POST /api/admin/users/u8/ban": () => json({ detail: "Недостаточно прав" }, 403),
      },
      { user: STAFF },
    );
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Заблокировать автора" }));
    fireEvent.change(screen.getByLabelText(/Причина блокировки/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать" }));
    expect(await screen.findByText("Недостаточно прав")).toBeInTheDocument();
  });
});

describe("Comments: ревью и аудит шага 2", () => {
  it("добавление во время подгрузки следующей страницы не теряется", async () => {
    let releasePage: () => void = () => undefined;
    api({
      "GET /api/videos/v1/comments": async (url: URL) => {
        if (url.searchParams.get("cursor") === "c2") {
          await new Promise<void>((r) => (releasePage = r));
          return { items: [comment(3)], next_cursor: null };
        }
        return { items: [comment(1), comment(2)], next_cursor: "c2" };
      },
      "POST /api/videos/v1/comments": () => json(comment(9, { body: "Новый", can_delete: true }), 201),
    });
    render();
    await screen.findByText("Комментарий 2");
    fireEvent.click(screen.getByRole("button", { name: "Показать ещё" }));
    fireEvent.change(screen.getByLabelText("Комментарий"), { target: { value: "Новый" } });
    fireEvent.click(screen.getByRole("button", { name: "Комментировать" }));
    expect(await screen.findByText("Новый")).toBeInTheDocument();
    releasePage();
    await act(async () => undefined);
    expect(screen.getByText("Новый")).toBeInTheDocument();
    // Отменённая подгрузка — снова доступна.
    fireEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument());
    releasePage();
    expect(await screen.findByText("Комментарий 3")).toBeInTheDocument();
    expect(screen.getByText("Новый")).toBeInTheDocument();
  });

  it("staff удаляет комментарий заблокированного: счётчик не уменьшается (сервер их не считает)", async () => {
    const getCard = vi.fn(() => card({ comments_count: 2 }));
    api(
      {
        "GET /api/videos/v1": getCard,
        "GET /api/videos/v1/comments": () => ({
          items: [comment(1, { can_delete: true, author_user_id: "u7", author_banned: true }), comment(2), comment(3)],
          next_cursor: null,
        }),
        "DELETE /api/comments/k1": () => new Response(null, { status: 204 }),
      },
      { user: STAFF },
    );
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    fireEvent.click(within(list()).getAllByRole("button", { name: "Удалить" })[0]);
    await waitFor(() => expect(screen.queryByText("Комментарий 1")).not.toBeInTheDocument());
    await waitFor(() => expect(getCard).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("heading", { name: "2 комментария" })).toBeInTheDocument();
  });

  it("удаление 404 (уже удалён) — убран без сообщения, карточка перезапрошена", async () => {
    const getCard = vi.fn(() => card());
    api({
      "GET /api/videos/v1": getCard,
      "GET /api/videos/v1/comments": () => ({ items: [comment(1, { can_delete: true })], next_cursor: null }),
      "DELETE /api/comments/k1": () => json({ detail: "Комментарий не найден" }, 404),
    });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    fireEvent.click(within(list()).getAllByRole("button", { name: "Удалить" })[0]);
    await waitFor(() => expect(screen.queryByText("Комментарий 1")).not.toBeInTheDocument());
    await waitFor(() => expect(getCard).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Комментарий не найден")).not.toBeInTheDocument();
  });

  it("подтверждение удаления: фокус на «Удалить», «Отмена» — без запроса, фокус обратно", async () => {
    const del = vi.fn();
    api({
      "GET /api/videos/v1/comments": () => ({ items: [comment(1, { can_delete: true })], next_cursor: null }),
      "DELETE /api/comments/k1": del,
    });
    render();
    await screen.findByText("Комментарий 1");
    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));
    const group = screen.getByRole("group", { name: "Удалить комментарий?" });
    expect(within(group).getByRole("button", { name: "Удалить" })).toHaveFocus();
    fireEvent.click(within(group).getByRole("button", { name: "Отмена" }));
    expect(screen.getByRole("button", { name: "Удалить" })).toHaveFocus();
    expect(del).not.toHaveBeenCalled();
  });

  it("бан: перезапрошены карточка видео и список; «Отмена» возвращает фокус", async () => {
    const getCard = vi.fn(() => card());
    api(
      {
        "GET /api/videos/v1": getCard,
        "GET /api/videos/v1/comments": () => ({ items: [comment(2, { author_user_id: "u8", author_banned: false })], next_cursor: null }),
        "POST /api/admin/users/u8/ban": () => ({ user_id: "u8", username: "u", role: "user", is_banned: true, banned_reason: "x", banned_by_role: "moderator" }),
      },
      { user: STAFF },
    );
    render();
    const open = await screen.findByRole("button", { name: "Заблокировать автора" });
    fireEvent.click(open);
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(screen.getByRole("button", { name: "Заблокировать автора" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать автора" }));
    fireEvent.change(screen.getByLabelText(/Причина блокировки/), { target: { value: "спам" } });
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать" }));
    await waitFor(() => expect(getCard).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("status")).toHaveTextContent("Автор заблокирован");
  });

  it("ошибка «Показать ещё» — сообщение и «Повторить»", async () => {
    let fail = true;
    api({
      "GET /api/videos/v1/comments": (url: URL) =>
        url.searchParams.get("cursor") === "c2"
          ? fail
            ? json({ detail: "x" }, 500)
            : { items: [comment(3)], next_cursor: null }
          : { items: [comment(1)], next_cursor: "c2" },
    });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Не удалось загрузить продолжение")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Показать ещё" })).not.toBeInTheDocument();
    fail = false;
    const section = screen.getByRole("region", { name: /комментари/ });
    fireEvent.click(within(section).getByRole("button", { name: "Повторить" }));
    expect(await screen.findByText("Комментарий 3")).toBeInTheDocument();
  });

  it("409 при отправке (видео скрыли) — сообщение, текст сохранён; правка текста убирает ошибку", async () => {
    api({ "POST /api/videos/v1/comments": () => json({ detail: "Видео скрыто модератором" }, 409) });
    render();
    await screen.findByText("Комментарий 1");
    const field = screen.getByLabelText("Комментарий");
    fireEvent.change(field, { target: { value: "текст" } });
    fireEvent.click(screen.getByRole("button", { name: "Комментировать" }));
    expect(await screen.findByText("Видео скрыто модератором")).toBeInTheDocument();
    expect(field).toHaveValue("текст");
    fireEvent.change(field, { target: { value: "текст!" } });
    expect(screen.queryByText("Видео скрыто модератором")).not.toBeInTheDocument();
  });

  it("длинный комментарий свёрнут, «Читать полностью» с aria-expanded; короткий — без кнопки", async () => {
    const long = Array.from({ length: 12 }, (_, i) => `строка ${i}`).join("\n");
    api({ "GET /api/videos/v1/comments": () => ({ items: [comment(1, { body: long }), comment(2, { body: "x".repeat(600) })], next_cursor: null }) });
    render();
    const more = await screen.findByRole("button", { name: "Читать полностью" });
    expect(screen.getAllByRole("button", { name: "Читать полностью" })).toHaveLength(1);
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(screen.getByRole("button", { name: "Свернуть" })).toHaveAttribute("aria-expanded", "true");
  });

  it("смена пользователя в другой вкладке (/me при возврате): поля staff и черновик сброшены", async () => {
    let me: object = STAFF;
    api({
      "GET /api/auth/me": () => me,
      "GET /api/videos/v1/comments": () =>
        me === STAFF
          ? { items: [comment(2, { author_user_id: "u8", author_banned: false, can_delete: true })], next_cursor: null }
          : { items: [comment(2)], next_cursor: null },
    });
    render();
    await screen.findByRole("button", { name: "Заблокировать автора" });
    fireEvent.change(screen.getByLabelText("Комментарий"), { target: { value: "черновик модератора" } });
    me = ME;
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Заблокировать автора" })).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Удалить" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Комментарий")).toHaveValue("");
    focusManager.setFocused(undefined);
  });
});
