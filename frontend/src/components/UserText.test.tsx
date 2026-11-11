import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { UserText } from "./UserText";

describe("UserText", () => {
  it("HTML выводится как текст, не разбирается", () => {
    const payload = '<img src=x onerror="alert(1)"><b>жирный</b>';
    const { container } = render(<UserText text={payload} as="p" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText(payload)).toBeInTheDocument();
  });

  it("направление — auto (RTL-текст изолирован)", () => {
    render(<UserText text="שלום 123" />);
    expect(screen.getByText("שלום 123")).toHaveAttribute("dir", "auto");
  });

  it("обрезка по строкам и подсказка с полным текстом", () => {
    render(<UserText text="Длинное название" lines={2} title />);
    const el = screen.getByText("Длинное название");
    expect(el.style.getPropertyValue("--lines")).toBe("2");
    expect(el).toHaveAttribute("title", "Длинное название");
  });
});
