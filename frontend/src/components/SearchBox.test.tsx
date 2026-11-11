import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Link, useNavigate } from "react-router-dom";
import { SEARCH_DEBOUNCE_MS } from "../lib/useDebouncedValue";
import { mockApi, renderApp } from "../test/utils";
import { SearchBox } from "./SearchBox";

function type(value: string) {
  fireEvent.change(screen.getByRole("searchbox"), { target: { value } });
}

const location = () => screen.getByTestId("location").textContent;

describe("SearchBox", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockApi({ "GET /api/auth/me": () => new Response(null, { status: 401 }) });
  });
  afterEach(() => vi.useRealTimers());

  it("на главной: серия ввода → один переход после паузы", () => {
    renderApp(<SearchBox />);
    type("к");
    act(() => vi.advanceTimersByTime(100));
    type("ко");
    act(() => vi.advanceTimersByTime(100));
    type("кот");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 1));
    expect(location()).toBe("/");
    act(() => vi.advanceTimersByTime(1));
    expect(location()).toBe(`/?q=${encodeURIComponent("кот")}`);
  });

  it("пробелы по краям не меняют запрос; очистка возвращает ленту", () => {
    renderApp(<SearchBox />, { route: "/?q=кот" });
    expect(screen.getByRole("searchbox")).toHaveValue("кот");
    const before = location();
    type("  кот  ");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
    expect(location()).toBe(before); // запрос тот же — перехода нет
    fireEvent.click(screen.getByRole("button", { name: "Очистить запрос" }));
    expect(location()).toBe("/");
  });

  it("на другой странице ввод не уводит, Enter — переход на главную с q", () => {
    renderApp(<SearchBox />, { route: "/watch/v1" });
    type("пёс");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS * 2));
    expect(location()).toBe("/watch/v1");
    fireEvent.submit(screen.getByRole("search"));
    expect(location()).toBe(`/?q=${encodeURIComponent("пёс")}`);
  });

  it("поле ограничено 100 символами (как q в API)", () => {
    renderApp(<SearchBox />);
    expect(screen.getByRole("searchbox")).toHaveAttribute("maxLength", "100");
  });

  it("первый запрос — новая запись истории: «назад» возвращает ленту и пустое поле", () => {
    renderApp(<><SearchBox /><BackButton /></>);
    type("кот");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
    type("котик");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
    expect(location()).toBe(`/?q=${encodeURIComponent("котик")}`);
    fireEvent.click(screen.getByRole("button", { name: "назад" }));
    expect(location()).toBe("/");
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });

  it("уход на главную по ссылке очищает недонабранный запрос", () => {
    renderApp(<><SearchBox /><Link to="/">лого</Link></>, { route: "/watch/v1" });
    type("пёс");
    fireEvent.click(screen.getByRole("link", { name: "лого" }));
    expect(location()).toBe("/");
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });
});

function BackButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      назад
    </button>
  );
}

