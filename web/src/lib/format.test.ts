import { describe, expect, it } from "vitest";
import {
  formatConfidence,
  formatDate,
  formatMarketCap,
  hostOf,
  timeAgo,
} from "./format";

describe("formatMarketCap", () => {
  it("formats billions/trillions with £ prefix", () => {
    expect(formatMarketCap(236.1e9)).toBe("£236.1B");
    expect(formatMarketCap(1.2e12)).toBe("£1.20T");
    expect(formatMarketCap(45e6)).toBe("£45.0M");
  });
  it("returns an em dash for missing values", () => {
    expect(formatMarketCap(null)).toBe("—");
    expect(formatMarketCap(undefined)).toBe("—");
    expect(formatMarketCap(0)).toBe("—");
  });
});

describe("formatConfidence", () => {
  it("formats as a percentage", () => {
    expect(formatConfidence(0.9)).toBe("90%");
    expect(formatConfidence(0)).toBe("0%");
  });
  it("handles missing values", () => {
    expect(formatConfidence(null)).toBe("—");
  });
});

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
