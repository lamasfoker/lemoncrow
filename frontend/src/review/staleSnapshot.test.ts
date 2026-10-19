import { describe, expect, it } from "vitest";

import { STALE_SNAPSHOT_NOTICE, classifyWriteFailure, isStaleSnapshotError } from "./staleSnapshot";

const http = (status: number, message: string) => Object.assign(new Error(message), { status });

describe("staleSnapshot", () => {
  it("recognises the server's advanced-Review conflict", () => {
    expect(isStaleSnapshotError(http(409, "the Review advanced before mark; reload the current revision and try again"))).toBe(true);
  });
  it("does not treat other failures as stale", () => {
    expect(isStaleSnapshotError(http(422, "the Review advanced"))).toBe(false);
    expect(isStaleSnapshotError(http(409, "some other conflict"))).toBe(false);
    expect(isStaleSnapshotError(new Error("the Review advanced"))).toBe(false);
    expect(isStaleSnapshotError(null)).toBe(false);
  });
  it("classifies writes", () => {
    expect(classifyWriteFailure(http(409, "the Review advanced before finish"))).toEqual({ stale: true, message: STALE_SNAPSHOT_NOTICE });
    expect(classifyWriteFailure(new Error("boom"))).toEqual({ stale: false, message: "boom" });
    expect(classifyWriteFailure("plain")).toEqual({ stale: false, message: "plain" });
  });
});
