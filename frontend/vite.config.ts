import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { brotliCompress, constants as zlib, gzip } from "node:zlib";
import type { Plugin } from "vite";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_URL || "http://127.0.0.1:7420";

/**
 * Write `.br` and `.gz` siblings for text assets. The servers hand them out
 * through aiohttp's FileResponse (Content-Encoding + Vary), so a 4 MB editor
 * chunk crosses the wire at a fraction of its size with no runtime CPU.
 * Keep in sync with the hosted frontend build config.
 */
function precompress(): Plugin {
  const brotli = promisify(brotliCompress);
  const gz = promisify(gzip);
  let outDir = "dist";
  return {
    name: "lemoncrow-precompress",
    apply: "build",
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir);
    },
    async writeBundle(_options, bundle) {
      const files = Object.keys(bundle).filter((file) => /\.(?:js|mjs|css|html|svg|json|wasm)$/.test(file));
      await Promise.all(
        files.map(async (file) => {
          const source = await readFile(path.join(outDir, file));
          if (source.length < 1024) return;
          const [br, gzipped] = await Promise.all([
            brotli(source, {
              params: {
                [zlib.BROTLI_PARAM_QUALITY]: zlib.BROTLI_MAX_QUALITY,
                [zlib.BROTLI_PARAM_SIZE_HINT]: source.length,
              },
            }),
            gz(source, { level: 9 }),
          ]);
          if (br.length < source.length) await writeFile(path.join(outDir, `${file}.br`), br);
          if (gzipped.length < source.length) await writeFile(path.join(outDir, `${file}.gz`), gzipped);
        }),
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), precompress()],
  build: {
    chunkSizeWarningLimit: 2000,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    globals: true,
    // e2e/ holds the Playwright browser harness (npm run test:e2e), not unit tests.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
  server: {
    host: "0.0.0.0",
    port: 3125,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 3125,
  },
});
