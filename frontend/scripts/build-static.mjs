// Produce a self-contained public UI for FastAPI. The development API proxy is
// deliberately omitted: production calls FastAPI at the browser's own origin.
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const expectedNode = readFileSync(join(root, ".node-version"), "utf8").trim();
if (process.versions.node !== expectedNode)
  throw new Error(`Build requires Node ${expectedNode}`);
const stage = mkdtempSync(join(tmpdir(), "lexvault-next-export-"));
function inputs(directory) {
  return readdirSync(directory, { withFileTypes: true })
    .sort((a, b) => a.name.localeCompare(b.name))
    .flatMap((entry) => {
      const path = join(directory, entry.name);
      if (entry.isSymbolicLink())
        throw new Error("Refusing a symlink in frontend sources");
      return entry.isDirectory() ? inputs(path) : [path];
    });
}
try {
  const sources = [
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "next-env.d.ts",
    ".node-version",
    "scripts/build-static.mjs",
  ]
    .map((name) => join(root, name))
    .concat(inputs(join(root, "src")));
  const identity = sources.map((path) => ({
    path: path.slice(root.length + 1),
    sha256: createHash("sha256").update(readFileSync(path)).digest("hex"),
  }));
  for (const name of ["package.json", "tsconfig.json", "next-env.d.ts", "src"])
    cpSync(join(root, name), join(stage, name), { recursive: true });
  rmSync(join(stage, "src/app/api"), { recursive: true, force: true });
  symlinkSync(join(root, "node_modules"), join(stage, "node_modules"), "dir");
  writeFileSync(
    join(stage, "next.config.mjs"),
    'export default { output: "export", poweredByHeader: false, images: { unoptimized: true } };\n',
  );
  const result = spawnSync(
    process.execPath,
    [join(root, "node_modules/next/dist/bin/next"), "build", "--webpack"],
    {
      cwd: stage,
      stdio: "inherit",
      env: { ...process.env, NEXT_TELEMETRY_DISABLED: "1" },
    },
  );
  if (result.status !== 0)
    throw new Error(`Static build failed (${result.status})`);
  const revision = spawnSync("git", ["rev-parse", "HEAD"], {
    cwd: root,
    encoding: "utf8",
  });
  const status = spawnSync(
    "git",
    ["status", "--porcelain", "--untracked-files=normal"],
    { cwd: root, encoding: "utf8" },
  );
  const exportRoot = join(stage, "out");
  const publicAssets = [
    join(exportRoot, "index.html"),
    ...inputs(join(exportRoot, "_next")),
  ].map((path) => ({
    path: path.slice(exportRoot.length + 1),
    sha256: createHash("sha256").update(readFileSync(path)).digest("hex"),
  }));
  writeFileSync(
    join(stage, "out/build-info.json"),
    JSON.stringify(
      {
        format_version: 1,
        node: process.versions.node,
        revision:
          revision.status === 0 ? revision.stdout.trim() : "unavailable",
        dirty: status.status === 0 ? Boolean(status.stdout.trim()) : null,
        input_sha256: createHash("sha256")
          .update(JSON.stringify(identity))
          .digest("hex"),
        files: identity,
        assets: publicAssets,
      },
      null,
      2,
    ) + "\n",
  );
  const destination = join(root, "out"),
    previous = join(root, "out.previous");
  for (const path of [destination, previous])
    if (existsSync(path) && lstatSync(path).isSymbolicLink())
      throw new Error("Refusing symlink output directory");
  // Copy before replacing a working build. Compilation failure leaves it intact.
  const prepared = mkdtempSync(join(root, ".export-"));
  try {
    cpSync(join(stage, "out"), prepared, { recursive: true });
    if (existsSync(destination)) {
      rmSync(previous, { recursive: true, force: true });
      renameSync(destination, previous);
    }
    try {
      renameSync(prepared, destination);
    } catch (error) {
      if (existsSync(previous) && !existsSync(destination))
        renameSync(previous, destination);
      throw error;
    }
  } finally {
    rmSync(prepared, { recursive: true, force: true });
  }
  console.log(
    "Static UI ready: frontend/out (previous build retained when present)",
  );
} finally {
  rmSync(stage, { recursive: true, force: true });
}
