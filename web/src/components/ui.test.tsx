import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Badge, Button, Dialog, EmptyState, impactTone, sentimentTone } from "./ui";

describe("design system", () => {
  it("maps sentiment/impact to badge tones", () => {
    expect(sentimentTone("positive")).toBe("up");
    expect(sentimentTone("negative")).toBe("down");
    expect(sentimentTone("neutral")).toBe("mixed");
    expect(impactTone("high")).toBe("down");
    expect(impactTone("low")).toBe("neutral");
  });

  it("renders badges with accessible text", () => {
    render(<Badge tone="up">positive</Badge>);
    expect(screen.getByText("positive")).toBeInTheDocument();
  });

  it("renders an empty state with action", () => {
    render(
      <EmptyState
        title="No reports yet"
        message="Start a run."
        action={<Button>Start</Button>}
      />
    );
    expect(screen.getByText("No reports yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });

  it("dialog renders on open and closes on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <Dialog open={false} onClose={onClose} title="Details">
        body
      </Dialog>
    );
    expect(screen.queryByText("body")).not.toBeInTheDocument();

    rerender(
      <Dialog open={true} onClose={onClose} title="Details">
        body
      </Dialog>
    );
    expect(screen.getByText("body")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
