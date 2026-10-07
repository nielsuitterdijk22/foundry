import { describe, expect, it } from "vitest";
import { greeting } from "./index.js";

describe("greeting", () => {
  it("returns the project name", () => {
    expect(greeting()).toBe("{{name}}");
  });
});
