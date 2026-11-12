import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireStaff } from "../../auth/RequireStaff";
import { Layout } from "../../components/Layout";
import { json, ME, mockApi, renderApp, type Handler, type MockRoutes } from "../../test/utils";
import { ModerationPage } from "./ModerationPage";

const MOD = { ...ME, id: "m1", username: "mod", role: "moderator", channel: { id: "cm", handle: "mod", title: "Мод" } };

function report(n: number, extra: Record<string, unknown> = {}, video: Record<string, unknown> = {}) {
  return {
    id: `r${n}`,
    category: "spam",
    comment: null,
    status: "open",
    created_at: "2026-11-12T10:00:00Z",
    reporter: { id: "u9", username: "reporter" },
    video: {
      id: `v${n}`,
      title: `Видео ${n}`,
      channel_handle: "bob",
      moderation: "ok",
      visibility: "public",
      status: "ready",
      owner_id: "o1",
      owner_banned: false,
      ...video,
    },
    resolved_by: null,
    resolved_at: null,
    ...extra,
  };
}

const anon: Handler = () => json({ detail: "Требуется вход" }, 401);

function api(routes: MockRoutes = {}, user: object | null = MOD) {
  return mockApi({
    "GET /api/auth/me": user ? () => user : anon,
    "GET /api/admin/reports": () => ({ items: [report(1)], next_cursor: null }),
    ...routes,
  });
}

function render(route = "/moderation") {
  return renderApp(
    <Routes>
      <Route
        path="/moderation"
        element={
          <RequireStaff>
            <ModerationPage />
          </RequireStaff>
        }
      />
      <Route path="/login" element={<p>Страница входа</p>} />
    </Routes>,
    { route },
  );
}

const card = async (title: string) => (await screen.findByText(title)).closest("li") as HTMLElement;
const bodyOf = (init: RequestInit) => (init.body === undefined ? undefined : JSON.parse(String(init.body)));

afterEach(() => vi.unstubAllGlobals());

describe("Модерация: доступ к странице (только интерфейс — права проверяет API)", () => {
  it("аноним — «Страница не найдена», как на несуществующем адресе; очередь не запрашивается", async () => {
    const list = vi.fn(() => ({ items: [], next_cursor: null }));
    api({ "GET /api/admin/reports": list }, null);
    render();
    expect(await screen.findByText("Страница не найдена")).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it("обычный пользователь — «Страница не найдена», очередь не запрашивается", async () => {
    const list = vi.fn(() => ({ items: [], next_cursor: null }));
    api({ "GET /api/admin/reports": list }, ME);
    render();
    expect(await screen.findByText("Страница не найдена")).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it.each([MOD, { ...MOD, role: "admin" }])("staff (%s.role) — очередь жалоб", async (user) => {
    api({}, user);
    render();
    expect(await screen.findByText("Видео 1")).toBeInTheDocument();
  });

  it("пункт меню «Модерация» — только staff", async () => {
    api({}, ME);
    const { unmount } = renderApp(
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<p>главная</p>} />
        </Route>
      </Routes>,
    );
    await screen.findByRole("link", { name: "Мои видео" });
    expect(screen.queryByRole("link", { name: "Модерация" })).not.toBeInTheDocument();
    unmount();
    vi.unstubAllGlobals();
    api({}, MOD);
    renderApp(
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<p>главная</p>} />
        </Route>
      </Routes>,
    );
    expect(await screen.findByRole("link", { name: "Модерация" })).toHaveAttribute("href", "/moderation");
  });
});

describe("Модерация: очередь", () => {
  it("пояснение, ник жалобщика и название — текстом с dir=auto; удалённое видео — без ссылки", async () => {
    api({
      "GET /api/admin/reports": () => ({
        items: [
          report(1, { comment: "<img src=x onerror=alert(1)>", reporter: { id: "u", username: "שלום" } }),
          report(2, {}, { id: null, title: "Удалённое", moderation: null, visibility: null, status: null, owner_id: null, owner_banned: null }),
        ],
        next_cursor: null,
      }),
    });
    const { container } = render();
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toHaveAttribute("dir", "auto");
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("שלום")).toHaveAttribute("dir", "auto");
    expect(screen.getByRole("link", { name: "Видео 1" })).toHaveAttribute("href", "/watch/v1");
    const deleted = await card("Удалённое");
    expect(within(deleted).queryByRole("link")).not.toBeInTheDocument();
    expect(within(deleted).queryByRole("button", { name: "Скрыть видео" })).not.toBeInTheDocument();
    expect(within(deleted).queryByRole("button", { name: "Заблокировать владельца" })).not.toBeInTheDocument();
  });

  it("фильтр по статусу — запрос с status и адрес", async () => {
    const list = vi.fn((url: URL) => ({
      items: url.searchParams.get("status") === "dismissed" ? [report(5, { status: "dismissed" })] : [report(1)],
      next_cursor: null,
    }));
    api({ "GET /api/admin/reports": list });
    render();
    await screen.findByText("Видео 1");
    fireEvent.click(screen.getByRole("button", { name: "Отклонённые" }));
    expect(await screen.findByText("Видео 5")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Отклонённые" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("location")).toHaveTextContent("/moderation?status=dismissed");
    // У закрытой жалобы нет «Отклонить».
    expect(within(await card("Видео 5")).queryByRole("button", { name: "Отклонить жалобу" })).not.toBeInTheDocument();
  });

  it("пусто — «Открытых жалоб нет»", async () => {
    api({ "GET /api/admin/reports": () => ({ items: [], next_cursor: null }) });
    render();
    expect(await screen.findByText("Открытых жалоб нет")).toBeInTheDocument();
  });
});

describe("Модерация: действия", () => {
  it("скрыть: причина обязательна; PUT {state: hidden, reason}; очередь перезапрошена", async () => {
    let calls = 0;
    const put = vi.fn((_u: URL, init: RequestInit) => {
      expect(bodyOf(init)).toEqual({ state: "hidden", reason: "спам" });
      return { video_id: "v1", moderation: "hidden", moderation_reason: "спам", closed_reports: 1 };
    });
    api({
      "GET /api/admin/reports": () => (++calls, { items: calls === 1 ? [report(1)] : [], next_cursor: null }),
      "PUT /api/admin/videos/v1/moderation": put,
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" }));
    const field = screen.getByLabelText(/Причина скрытия/);
    expect(field).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
    expect(screen.getByText("Укажите причину")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "  спам  " } });
    fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
    expect(await screen.findByText("Открытых жалоб нет")).toBeInTheDocument();
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("вернуть видео: подтверждение, PUT {state: ok, reason: null}", async () => {
    const put = vi.fn((_u: URL, init: RequestInit) => {
      expect(bodyOf(init)).toEqual({ state: "ok", reason: null });
      return { video_id: "v1", moderation: "ok", moderation_reason: null, closed_reports: 0 };
    });
    api({
      "GET /api/admin/reports": () => ({ items: [report(1, { status: "resolved" }, { moderation: "hidden" })], next_cursor: null }),
      "PUT /api/admin/videos/v1/moderation": put,
    });
    render();
    expect(within(await card("Видео 1")).queryByRole("button", { name: "Скрыть видео" })).not.toBeInTheDocument();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Вернуть видео" }));
    const confirm = screen.getByRole("button", { name: "Вернуть" });
    expect(confirm).toHaveFocus();
    fireEvent.click(confirm);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
  });

  it("отклонить: без причины — POST без тела; с причиной — {reason}", async () => {
    const bodies: unknown[] = [];
    const post = vi.fn((_u: URL, init: RequestInit) => {
      bodies.push(bodyOf(init));
      return { id: "r1", status: "dismissed", resolved_by: "m1", resolved_at: "x" };
    });
    api({
      "GET /api/admin/reports": () => ({ items: [report(1), report(2)], next_cursor: null }),
      "POST /api/admin/reports/r1/dismiss": post,
      "POST /api/admin/reports/r2/dismiss": post,
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Отклонить жалобу" }));
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    fireEvent.click(within(await card("Видео 2")).getByRole("button", { name: "Отклонить жалобу" }));
    fireEvent.change(screen.getByLabelText(/Причина отклонения/), { target: { value: "не нарушение" } });
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(bodies).toEqual([undefined, { reason: "не нарушение" }]);
  });

  it("заблокировать владельца — PUT бана с причиной; разблокировать — DELETE", async () => {
    let banned = false;
    const put = vi.fn((_u: URL, init: RequestInit) => {
      expect(bodyOf(init)).toEqual({ reason: "нарушения" });
      banned = true;
      return { user_id: "o1", username: "bob", role: "user", is_banned: true, banned_reason: "нарушения", banned_by_role: "moderator", recount_videos: 1, recount_channels: 0 };
    });
    const del = vi.fn(() => {
      banned = false;
      return { user_id: "o1", username: "bob", role: "user", is_banned: false, banned_reason: null, banned_by_role: null, recount_videos: 1, recount_channels: 0 };
    });
    api({
      "GET /api/admin/reports": () => ({ items: [report(1, {}, { owner_banned: banned })], next_cursor: null }),
      "PUT /api/admin/users/o1/ban": put,
      "DELETE /api/admin/users/o1/ban": del,
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Заблокировать владельца" }));
    fireEvent.change(screen.getByLabelText(/Причина блокировки/), { target: { value: "нарушения" } });
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать" }));
    expect(await screen.findByText("владелец заблокирован")).toBeInTheDocument();
    expect(within(await card("Видео 1")).queryByRole("button", { name: "Заблокировать владельца" })).not.toBeInTheDocument();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Разблокировать владельца" }));
    fireEvent.click(screen.getByRole("button", { name: "Разблокировать" }));
    await waitFor(() => expect(screen.queryByText("владелец заблокирован")).not.toBeInTheDocument());
    expect(put).toHaveBeenCalledTimes(1);
    expect(del).toHaveBeenCalledTimes(1);
  });

  it("403 (своё видео модератора, бан админа) — сообщение сервера, форма остаётся", async () => {
    api({ "PUT /api/admin/videos/v1/moderation": () => json({ detail: "Нельзя модерировать своё видео" }, 403) });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" }));
    fireEvent.change(screen.getByLabelText(/Причина скрытия/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Нельзя модерировать своё видео");
    expect(screen.getByLabelText(/Причина скрытия/)).toBeInTheDocument();
  });

  it("«Отмена» — без запроса, фокус обратно на кнопку", async () => {
    const put = vi.fn();
    api({ "PUT /api/admin/videos/v1/moderation": put });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" }));
    fireEvent.click(screen.getByRole("button", { name: "Отмена" }));
    expect(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" })).toHaveFocus();
    expect(put).not.toHaveBeenCalled();
  });

  it("после успеха — фокус на заголовок, итог объявлен; очередь перезапрошена", async () => {
    let calls = 0;
    api({
      "GET /api/admin/reports": () => (++calls, { items: calls === 1 ? [report(1)] : [], next_cursor: null }),
      "POST /api/admin/reports/r1/dismiss": () => ({ id: "r1", status: "dismissed", resolved_by: "m1", resolved_at: "x" }),
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Отклонить жалобу" }));
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    expect(await screen.findByText("Открытых жалоб нет")).toBeInTheDocument();
    expect(calls).toBe(2);
    expect(screen.getByRole("heading", { name: "Модерация: жалобы" })).toHaveFocus();
    expect(screen.getByText("Жалоба отклонена", { selector: "[role=status]" })).toBeInTheDocument();
  });

  it("409 (уже обработал другой модератор) — очередь всё равно обновлена, итог объявлен", async () => {
    let calls = 0;
    api({
      "GET /api/admin/reports": () => (++calls, { items: calls === 1 ? [report(1)] : [], next_cursor: null }),
      "POST /api/admin/reports/r1/dismiss": () => json({ detail: "Жалоба уже закрыта" }, 409),
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Отклонить жалобу" }));
    fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
    expect(await screen.findByText("Открытых жалоб нет")).toBeInTheDocument();
    expect(screen.getByText("Жалоба уже закрыта", { selector: "[role=status]" })).toBeInTheDocument();
  });

  it("бан: причина обязательна (пробелы — без запроса); 422 по полю reason — у формы", async () => {
    const put = vi.fn(() => json({ detail: "x", errors: [{ loc: ["body", "reason"], msg: "Причина без видимых символов" }] }, 422));
    api({ "PUT /api/admin/users/o1/ban": put });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Заблокировать владельца" }));
    const field = screen.getByLabelText(/Причина блокировки/);
    fireEvent.change(field, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать" }));
    expect(screen.getByText("Укажите причину")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "\u200b x" } });
    fireEvent.click(screen.getByRole("button", { name: "Заблокировать" }));
    expect(await screen.findByText("Причина без видимых символов")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-describedby");
  });

  it("во время запроса «Отмена» недоступна", async () => {
    let release: () => void = () => undefined;
    api({
      "PUT /api/admin/videos/v1/moderation": async () => {
        await new Promise<void>((r) => (release = r));
        return { video_id: "v1", moderation: "hidden", moderation_reason: "x", closed_reports: 1 };
      },
    });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" }));
    fireEvent.change(screen.getByLabelText(/Причина скрытия/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Отмена" })).toBeDisabled());
    release();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Отмена" })).not.toBeInTheDocument());
  });

  it("своё видео (админ) — без кнопок бана владельца", async () => {
    const ADMIN = { ...MOD, id: "o1", role: "admin" };
    api({}, ADMIN);
    render();
    const own = await card("Видео 1");
    expect(within(own).queryByRole("button", { name: "Заблокировать владельца" })).not.toBeInTheDocument();
    expect(within(own).getByRole("button", { name: "Скрыть видео" })).toBeInTheDocument();
  });

  it("401 при действии — интерфейс анонима (страница скрыта)", async () => {
    api({ "PUT /api/admin/videos/v1/moderation": () => json({ detail: "Требуется вход" }, 401) });
    render();
    fireEvent.click(within(await card("Видео 1")).getByRole("button", { name: "Скрыть видео" }));
    fireEvent.change(screen.getByLabelText(/Причина скрытия/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Скрыть" }));
    expect(await screen.findByText("Страница не найдена")).toBeInTheDocument();
  });
});

describe("Модерация: фильтр и подгрузка", () => {
  it("?status=resolved — свой заголовок пустого состояния; мусорный status — открытые", async () => {
    const list = vi.fn((url: URL) => {
      expect(["open", "resolved"]).toContain(url.searchParams.get("status"));
      return { items: [], next_cursor: null };
    });
    api({ "GET /api/admin/reports": list });
    const { unmount } = render("/moderation?status=resolved");
    expect(await screen.findByText("Жалоб нет")).toBeInTheDocument();
    unmount();
    render("/moderation?status=foo");
    expect(await screen.findByText("Открытых жалоб нет")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Открытые" })).toHaveAttribute("aria-pressed", "true");
  });

  it("«Показать ещё» передаёт курсор", async () => {
    api({
      "GET /api/admin/reports": (url: URL) =>
        url.searchParams.get("cursor") === "c2" ? { items: [report(3)], next_cursor: null } : { items: [report(1)], next_cursor: "c2" },
    });
    render();
    fireEvent.click(await screen.findByRole("button", { name: "Показать ещё" }));
    expect(await screen.findByText("Видео 3")).toBeInTheDocument();
  });
});
