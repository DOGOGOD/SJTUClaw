// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SessionModeBadges } from "./ThreadShell";

afterEach(cleanup);

describe("SessionModeBadges", () => {
  it("shows the Pi indicator only for sessions with Pi enabled", () => {
    const view = render(
      <SessionModeBadges autoMode={false} unlimitedMode={false} piMode />
    );
    expect(view.getByTestId("pi-mode-badge").textContent).toContain("Pi");

    view.rerender(
      <SessionModeBadges autoMode={false} unlimitedMode={false} piMode={false} />
    );
    expect(view.queryByTestId("pi-mode-badge")).toBeNull();
  });
});
