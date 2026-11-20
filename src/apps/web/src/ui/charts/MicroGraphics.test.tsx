import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RangeBar, Sparkline } from "./MicroGraphics";

describe("MicroGraphics", () => {
  it("renders Sparkline with pure CSS classes, handles flat/empty inputs cleanly, and emits zero inline styles", () => {
    const { container, rerender } = render(
      <Sparkline values={[10, 14, 12, 18]} tone="positive" ariaLabel="收入上升趋势" />,
    );

    const svg = screen.getByRole("img", { name: "收入上升趋势" });
    expect(svg.classList.contains("micro-sparkline")).toBe(true);
    expect(svg.classList.contains("is-positive")).toBe(true);
    const poly = svg.querySelector("polyline");
    expect(poly?.getAttribute("points")).toBeTruthy();
    expect(poly?.getAttribute("points")).not.toContain("NaN");
    expect(container.querySelectorAll("[style]")).toHaveLength(0);

    // Equal values -> centered flat line without NaN division by zero
    rerender(<Sparkline values={[42, 42, 42]} tone="negative" ariaLabel="持平趋势" />);
    const flatPoints = screen.getByRole("img", { name: "持平趋势" }).querySelector("polyline")?.getAttribute("points");
    expect(flatPoints).toBeTruthy();
    expect(flatPoints).not.toContain("NaN");

    // Empty / <2 finite values -> baseline without crashing
    rerender(<Sparkline values={[Number.NaN]} ariaLabel="空趋势" />);
    expect(screen.getByRole("img", { name: "空趋势" }).querySelector("line")).toBeTruthy();
  });

  it("renders RangeBar interval and optional marker cleanly with zero inline styles", () => {
    const { container } = render(
      <RangeBar min={0} max={100} low={25} high={70} marker={52} tone="negative" ariaLabel="概率区间 25%-70%" />,
    );

    const svg = screen.getByRole("img", { name: "概率区间 25%-70%" });
    expect(svg.classList.contains("micro-range")).toBe(true);
    expect(svg.classList.contains("is-negative")).toBe(true);
    expect(svg.querySelector(".micro-range-track")).toBeTruthy();
    expect(svg.querySelector(".micro-range-band")).toBeTruthy();
    expect(svg.querySelector(".micro-range-marker")).toBeTruthy();
    expect(container.querySelectorAll("[style]")).toHaveLength(0);
  });
});
