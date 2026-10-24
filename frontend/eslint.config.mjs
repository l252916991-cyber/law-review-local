import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

export default defineConfig([...nextVitals, ...nextTs,
  globalIgnores([".next/**", "out/**", "out.previous/**", ".export-*/**", "next-env.d.ts", "src/lib/schema.d.ts", "playwright-report/**", "test-results/**"]),
]);
