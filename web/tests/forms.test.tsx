import { render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { globSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { Form } from "@/components/Form";

// A raw <form> is live in the server HTML before React hydrates it (typing is lost, Enter or a
// click submits it natively). Every form goes through Form; this list is the reviewed exception.
const RAW_FORM_ALLOWED = ["components/Form.tsx"];

const fields = (
  <>
    <input aria-label="Name" />
    <button type="submit">Go</button>
  </>
);

describe("forms are inert until hydrated", () => {
  it("no <form> outside Form in app/ or components/", () => {
    const sources = globSync("{app,components}/**/*.tsx").filter((file) => !RAW_FORM_ALLOWED.includes(file));
    expect(sources.filter((file) => /<form[\s>]/.test(readFileSync(file, "utf8")))).toEqual([]);
  });

  it("the server HTML disables every field and button", () => {
    const html = renderToString(<Form>{fields}</Form>);
    expect(html).toMatch(/<fieldset[^>]*disabled/);
  });

  it("once rendered in the browser the form is live", () => {
    render(<Form>{fields}</Form>);
    expect((screen.getByRole("button", { name: "Go" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText("Name") as HTMLInputElement).disabled).toBe(false);
  });
});
