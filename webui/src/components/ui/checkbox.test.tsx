// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Checkbox } from "./checkbox";

afterEach(cleanup);

describe("Checkbox", () => {
  it("uses the theme-aware native checkbox treatment", () => {
    const view = render(<Checkbox aria-label="测试复选框" />);
    const checkbox = view.getByRole("checkbox", { name: "测试复选框" });

    expect(checkbox.getAttribute("type")).toBe("checkbox");
    expect(checkbox.classList.contains("theme-checkbox")).toBe(true);
    expect(checkbox.classList.contains("accent-primary")).toBe(true);
  });

  it("preserves the checked and disabled states", () => {
    const view = render(<Checkbox aria-label="测试复选框" checked disabled readOnly />);
    const checkbox = view.getByRole("checkbox", { name: "测试复选框" }) as HTMLInputElement;

    expect(checkbox.checked).toBe(true);
    expect(checkbox.disabled).toBe(true);
  });
});
