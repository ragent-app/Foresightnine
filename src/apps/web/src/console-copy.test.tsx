import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ConsolePanel } from "./components/MethodsPanel";

it("does not claim that every account routes through Cline", () => {
  const html = renderToStaticMarkup(createElement(ConsolePanel, { onExport: () => undefined }));
  expect(html).toContain("账户模型配置");
  expect(html).not.toContain("Cline pool");
  expect(html).not.toContain("学术不期明");
});
