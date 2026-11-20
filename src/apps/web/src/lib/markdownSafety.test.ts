import { describe, expect, it } from "vitest";
import { extractTableTsv, isNumericCellText, safeExternalHref } from "./markdownSafety";

describe("safeExternalHref", () => {
  it("allows only public https:// URLs without credentials", () => {
    expect(safeExternalHref("https://www.sec.gov/Archives/edgar/data/1/a.htm")).toEqual({
      href: "https://www.sec.gov/Archives/edgar/data/1/a.htm",
      host: "www.sec.gov",
    });
    expect(safeExternalHref("http://www.sec.gov/a")).toBeNull();
    expect(safeExternalHref("javascript:alert(1)")).toBeNull();
    expect(safeExternalHref("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeExternalHref("vbscript:msgbox(1)")).toBeNull();
    expect(safeExternalHref("file:///etc/passwd")).toBeNull();
    expect(safeExternalHref("https://user:pass@www.sec.gov/")).toBeNull();
    expect(safeExternalHref("https://localhost/admin")).toBeNull();
    expect(safeExternalHref("https://127.0.0.1:8000")).toBeNull();
    expect(safeExternalHref("https://169.254.169.254/latest/meta-data/")).toBeNull();
    expect(safeExternalHref("https://10.0.0.5/internal")).toBeNull();
    expect(safeExternalHref("https://192.168.1.1/")).toBeNull();
    expect(safeExternalHref("https://172.16.0.1/")).toBeNull();
    expect(safeExternalHref("https://[::1]/")).toBeNull();
    expect(safeExternalHref("https://intranet/secret")).toBeNull();
  });
});

describe("isNumericCellText", () => {
  it("detects financial numbers, currencies, ratios, ranges, and placeholders while ignoring text labels", () => {
    for (const sample of ["$130.5B", "+114.2%", "-3.4 pp", "48,612", "(1,234.50)", "12.5x", "USD 41,270", "$35.1B–$130.5B", "—", "N/A"]) {
      expect(isNumericCellText(sample)).toBe(true);
    }
    for (const label of ["FY2026", "Cloud Services", "2026年财报说明", "同比增速"]) {
      expect(isNumericCellText(label)).toBe(false);
    }
  });
});

describe("extractTableTsv", () => {
  it("serializes a DOM table into tab-separated rows", () => {
    const table = document.createElement("table");
    table.innerHTML =
      "<thead><tr><th>指标</th><th>FY2025</th><th>FY2026</th></tr></thead>" +
      "<tbody><tr><td>总收入\n(USD M)</td><td>41,270</td><td>48,612</td></tr></tbody>";
    expect(extractTableTsv(table)).toBe("指标\tFY2025\tFY2026\n总收入 (USD M)\t41,270\t48,612");
  });
});
