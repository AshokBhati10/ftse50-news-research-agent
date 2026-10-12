import { describe, expect, it } from "vitest";
import { formatDate, hostOf, timeAgo } from "./format";

describe("formatDate", () => {
  it("formats ISO dates in en-GB style", () => {
    expect(formatDate("2026-10-10T07:00:00+00:00")).toBe("10 Oct 2026");
  });
  it("handles missing/invalid input", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
  });
});

describe("timeAgo", () => {
  it("describes recent timestamps", () => {
    const fiveMinAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    expect(timeAgo(fiveMinAgo)).toBe("5m ago");
  });
  it("handles missing input", () => {
    expect(timeAgo(null)).toBe("—");
  });
});

describe("hostOf", () => {
  it("extracts hostnames and strips www", () => {
    expect(hostOf("https://www.bbc.co.uk/news/x")).toBe("bbc.co.uk");
  });
  it("falls back to the raw string", () => {
    expect(hostOf("not a url")).toBe("not a url");
  });
});
