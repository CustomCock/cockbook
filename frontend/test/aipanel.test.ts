// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/aipanel";

describe("KI-Panel – Markdown", () => {
  it("rendert fett, Code, Listen – ohne HTML aus der Antwort auszuführen", () => {
    const el = renderMarkdown("**Wand** bei `65000`\n- eins\n- zwei\n<img src=x onerror=alert(1)>");
    expect(el.querySelector("strong")?.textContent).toBe("Wand");
    expect(el.querySelector("code")?.textContent).toBe("65000");
    expect(el.querySelectorAll("li")).toHaveLength(2);
    expect(el.querySelector("img")).toBeNull();
    expect(el.textContent).toContain("<img");
  });
});
