// Every test runs as if the browser were in Los Angeles: business times must not depend on it.
process.env.TZ = "America/Los_Angeles";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup); // Testing Library cleans up by itself only with Vitest globals on
