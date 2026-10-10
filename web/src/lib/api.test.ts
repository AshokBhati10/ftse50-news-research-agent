import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function mockFetch(impl: () => Promise<Response>) {
  vi.stubGlobal("fetch", vi.fn(impl));
}

describe("api client", () => {
  it("throws ApiError(0) when the backend is unreachable", async () => {
    mockFetch(() => Promise.reject(new Error("ECONNREFUSED")));
    await expect(api.health()).rejects.toMatchObject({
      name: "ApiError",
      status: 0,
    } as Partial<ApiError>);
  });

  it("surfaces the backend's error detail", async () => {
    mockFetch(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "Unknown run: 'x'" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    const err = await api.run("x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("Unknown run: 'x'");
  });

  it("returns parsed JSON on success", async () => {
    mockFetch(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    const health = await api.health();
    expect(health.status).toBe("ok");
  });

  it("builds download URLs with encoded ids", () => {
    expect(api.downloadUrl("report_2026-10-10", "json")).toBe(
      "/api/reports/report_2026-10-10/download?format=json"
    );
  });
});
