import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import manifest from "../app/manifest";

const PUBLIC = join(__dirname, "../public");
const css = readFileSync(join(__dirname, "../app/globals.css"), "utf8");
const token = (name: string) => new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`).exec(css)![1].toLowerCase();

/** Width and height from a PNG's IHDR chunk (bytes 16-24). */
function pngSize(path: string): [number, number] {
  const bytes = readFileSync(path);
  expect(bytes.subarray(0, 8).toString("hex"), `${path} is a PNG`).toBe("89504e470d0a1a0a");
  return [bytes.readUInt32BE(16), bytes.readUInt32BE(20)];
}

describe("the web app manifest", () => {
  const m = manifest();

  it("names the app and starts it standalone at the root", () => {
    expect(m.name).toBe("QueueLess");
    expect(m.short_name).toBe("QueueLess");
    expect(m.display).toBe("standalone");
    expect(m.start_url).toBe("/");
    expect(m.scope).toBe("/");
  });

  it("takes its colours from the design tokens", () => {
    expect(m.theme_color?.toLowerCase()).toBe(token("--primary"));
    expect(m.background_color?.toLowerCase()).toBe(token("--background"));
  });

  it("has a 192, a 512 and a maskable icon, each a real PNG of that size", () => {
    const icons = m.icons ?? [];
    const find = (size: string, purpose: string) => icons.find((icon) => icon.sizes === size && (icon.purpose ?? "any") === purpose);
    for (const [size, purpose] of [["192x192", "any"], ["512x512", "any"], ["512x512", "maskable"]] as const) {
      const icon = find(size, purpose);
      expect(icon, `${size} ${purpose}`).toBeTruthy();
      expect(icon!.type).toBe("image/png");
      const [w, h] = pngSize(join(PUBLIC, icon!.src));
      expect(`${w}x${h}`).toBe(size);
    }
  });
});
