import { render, screen } from "@testing-library/react";
import Watchdogs from "./Watchdogs";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Watchdogs page", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows a coming-soon preview with no save controls", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation((input: RequestInfo | URL) => {
        if (String(input).includes("/api/watchdogs/config")) {
          return Promise.resolve(
            jsonResponse({
              active_profile: "coding",
              runtime_wired: false,
              config_path: "/tmp/.lemoncrow/watchdog_profiles.json",
              library: [
                {
                  key: "repeated_command_failure",
                  title: "Repeated command failure",
                  description:
                    "Same command or test fails repeatedly with the same error signature.",
                  default_weight: 0.3,
                  severity: "high",
                },
              ],
              profiles: [],
            })
          );
        }
        return Promise.resolve(new Response("not found", { status: 404 }));
      });

    render(<Watchdogs />);

    expect(screen.getByText("Coming soon")).toBeInTheDocument();
    expect(
      await screen.findByText("Repeated command failure")
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(
      fetchSpy.mock.calls.every(
        ([, init]) => !init?.method || init.method === "GET"
      )
    ).toBe(true);
  });
});
