import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Markdown, InlineMarkdown, renderBlocks } from "./markdown";

describe("Markdown renderer", () => {
  it("splits blank-line-separated text into separate paragraphs", () => {
    const { container } = render(
      <Markdown text={"First paragraph.\n\nSecond paragraph."} />
    );
    const ps = container.querySelectorAll("p");
    expect(ps).toHaveLength(2);
    expect(ps[0].textContent).toBe("First paragraph.");
    expect(ps[1].textContent).toBe("Second paragraph.");
  });

  it("renders bold as <strong> with no literal asterisks", () => {
    const { container } = render(<Markdown text={"Completed on **5 Oct 2026**."} />);
    const strong = container.querySelector("strong");
    expect(strong?.textContent).toBe("5 Oct 2026");
    expect(container.textContent).not.toContain("**");
  });

  it("renders italic and inline code", () => {
    const { container } = render(<Markdown text={"An *approximate* date and `AZN.L`."} />);
    expect(container.querySelector("em")?.textContent).toBe("approximate");
    expect(container.querySelector("code")?.textContent).toBe("AZN.L");
    expect(container.textContent).not.toContain("*");
  });

  it("renders safe links and rejects unsafe schemes", () => {
    const { container } = render(
      <Markdown text={"[Source](https://example.com/a) and [bad](javascript:alert(1))"} />
    );
    const link = container.querySelector("a");
    expect(link).toHaveAttribute("href", "https://example.com/a");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // The unsafe link keeps its label but is not a link.
    expect(container.querySelectorAll("a")).toHaveLength(1);
    expect(container.textContent).toContain("bad");
  });

  it("renders ordered, unordered and nested lists", () => {
    const md = [
      "1. First finding",
      "2. Second finding",
      "   - supporting detail",
      "   - more detail",
      "3. Third finding",
    ].join("\n");
    const { container } = render(<Markdown text={md} />);
    const ol = container.querySelector("ol");
    expect(ol).not.toBeNull();
    expect(ol!.querySelectorAll(":scope > li")).toHaveLength(3);
    expect(container.querySelectorAll("ul ul, ol ul, ul ol").length).toBeGreaterThan(0);
  });

  it("renders headings, rules and blockquotes", () => {
    const md = "## Key findings\n\n> A quoted caveat.\n\n---";
    const { container } = render(<Markdown text={md} />);
    expect(container.querySelector("h2")?.textContent).toBe("Key findings");
    expect(container.querySelector("blockquote")?.textContent).toContain("quoted caveat");
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("renders simple tables", () => {
    const md = "| Date | Event |\n|---|---|\n| 5 Oct 2026 | Completion |";
    const { container } = render(<Markdown text={md} />);
    expect(container.querySelectorAll("table th")).toHaveLength(2);
    expect(container.querySelectorAll("table td")).toHaveLength(2);
    expect(container.querySelector("td")?.textContent).toBe("5 Oct 2026");
  });

  it("treats embedded HTML as text, never markup", () => {
    const { container } = render(
      <Markdown text={"<script>alert(1)</script> & <b>x</b>"} />
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });

  it("renderBlocks handles a realistic dense answer", () => {
    const answer = [
      "AstraZeneca completed a **$2bn** equity investment in Summit Therapeutics.",
      "",
      "1. **5 Oct 2026** — completion reported.",
      "2. **2 Oct 2026** — collaboration announced.",
      "",
      "- Dates use relative timestamps (*approximate*).",
    ].join("\n");
    const nodes = renderBlocks(answer);
    const { container } = render(<Markdown text={answer} />);
    expect(nodes.length).toBeGreaterThan(2);
    const ps = container.querySelectorAll("p");
    expect(ps.length).toBeGreaterThanOrEqual(1);
    expect(container.textContent).not.toContain("**");
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("InlineMarkdown renders inline formatting only", () => {
    const { container } = render(<InlineMarkdown text={"The **CEO** said so."} />);
    expect(container.querySelector("strong")?.textContent).toBe("CEO");
    expect(container.querySelector("p")).toBeNull();
    expect(container.textContent).not.toContain("**");
  });
});
