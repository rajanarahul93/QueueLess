import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup); // Testing Library cleans up by itself only with Vitest globals on
