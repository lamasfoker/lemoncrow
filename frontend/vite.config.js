var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
var __generator = (this && this.__generator) || function (thisArg, body) {
    var _ = { label: 0, sent: function() { if (t[0] & 1) throw t[1]; return t[1]; }, trys: [], ops: [] }, f, y, t, g = Object.create((typeof Iterator === "function" ? Iterator : Object).prototype);
    return g.next = verb(0), g["throw"] = verb(1), g["return"] = verb(2), typeof Symbol === "function" && (g[Symbol.iterator] = function() { return this; }), g;
    function verb(n) { return function (v) { return step([n, v]); }; }
    function step(op) {
        if (f) throw new TypeError("Generator is already executing.");
        while (g && (g = 0, op[0] && (_ = 0)), _) try {
            if (f = 1, y && (t = op[0] & 2 ? y["return"] : op[0] ? y["throw"] || ((t = y["return"]) && t.call(y), 0) : y.next) && !(t = t.call(y, op[1])).done) return t;
            if (y = 0, t) op = [op[0] & 2, t.value];
            switch (op[0]) {
                case 0: case 1: t = op; break;
                case 4: _.label++; return { value: op[1], done: false };
                case 5: _.label++; y = op[1]; op = [0]; continue;
                case 7: op = _.ops.pop(); _.trys.pop(); continue;
                default:
                    if (!(t = _.trys, t = t.length > 0 && t[t.length - 1]) && (op[0] === 6 || op[0] === 2)) { _ = 0; continue; }
                    if (op[0] === 3 && (!t || (op[1] > t[0] && op[1] < t[3]))) { _.label = op[1]; break; }
                    if (op[0] === 6 && _.label < t[1]) { _.label = t[1]; t = op; break; }
                    if (t && _.label < t[2]) { _.label = t[2]; _.ops.push(op); break; }
                    if (t[2]) _.ops.pop();
                    _.trys.pop(); continue;
            }
            op = body.call(thisArg, _);
        } catch (e) { op = [6, e]; y = 0; } finally { f = t = 0; }
        if (op[0] & 5) throw op[1]; return { value: op[0] ? op[1] : void 0, done: true };
    }
};
var __spreadArray = (this && this.__spreadArray) || function (to, from, pack) {
    if (pack || arguments.length === 2) for (var i = 0, l = from.length, ar; i < l; i++) {
        if (ar || !(i in from)) {
            if (!ar) ar = Array.prototype.slice.call(from, 0, i);
            ar[i] = from[i];
        }
    }
    return to.concat(ar || Array.prototype.slice.call(from));
};
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { brotliCompress, constants as zlib, gzip } from "node:zlib";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
var apiTarget = process.env.VITE_API_URL || "http://127.0.0.1:7420";
/**
 * Write `.br` and `.gz` siblings for text assets. The servers hand them out
 * through aiohttp's FileResponse (Content-Encoding + Vary), so a 4 MB editor
 * chunk crosses the wire at a fraction of its size with no runtime CPU.
 * Keep in sync with the hosted frontend build config.
 */
function precompress() {
    var brotli = promisify(brotliCompress);
    var gz = promisify(gzip);
    var outDir = "dist";
    return {
        name: "lemoncrow-precompress",
        apply: "build",
        configResolved: function (config) {
            outDir = path.resolve(config.root, config.build.outDir);
        },
        writeBundle: function (_options, bundle) {
            return __awaiter(this, void 0, void 0, function () {
                var files;
                var _this = this;
                return __generator(this, function (_a) {
                    switch (_a.label) {
                        case 0:
                            files = Object.keys(bundle).filter(function (file) { return /\.(?:js|mjs|css|html|svg|json|wasm)$/.test(file); });
                            return [4 /*yield*/, Promise.all(files.map(function (file) { return __awaiter(_this, void 0, void 0, function () {
                                    var source, _a, br, gzipped;
                                    var _b;
                                    return __generator(this, function (_c) {
                                        switch (_c.label) {
                                            case 0: return [4 /*yield*/, readFile(path.join(outDir, file))];
                                            case 1:
                                                source = _c.sent();
                                                if (source.length < 1024)
                                                    return [2 /*return*/];
                                                return [4 /*yield*/, Promise.all([
                                                        brotli(source, {
                                                            params: (_b = {},
                                                                _b[zlib.BROTLI_PARAM_QUALITY] = zlib.BROTLI_MAX_QUALITY,
                                                                _b[zlib.BROTLI_PARAM_SIZE_HINT] = source.length,
                                                                _b),
                                                        }),
                                                        gz(source, { level: 9 }),
                                                    ])];
                                            case 2:
                                                _a = _c.sent(), br = _a[0], gzipped = _a[1];
                                                if (!(br.length < source.length)) return [3 /*break*/, 4];
                                                return [4 /*yield*/, writeFile(path.join(outDir, "".concat(file, ".br")), br)];
                                            case 3:
                                                _c.sent();
                                                _c.label = 4;
                                            case 4:
                                                if (!(gzipped.length < source.length)) return [3 /*break*/, 6];
                                                return [4 /*yield*/, writeFile(path.join(outDir, "".concat(file, ".gz")), gzipped)];
                                            case 5:
                                                _c.sent();
                                                _c.label = 6;
                                            case 6: return [2 /*return*/];
                                        }
                                    });
                                }); }))];
                        case 1:
                            _a.sent();
                            return [2 /*return*/];
                    }
                });
            });
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
        exclude: __spreadArray(__spreadArray([], configDefaults.exclude, true), ["e2e/**"], false),
    },
    server: {
        host: "0.0.0.0",
        port: 3125,
        proxy: {
            "/api": {
                target: apiTarget,
                changeOrigin: true,
                rewrite: function (path) { return path.replace(/^\/api/, ""); },
            },
        },
    },
    preview: {
        host: "0.0.0.0",
        port: 3125,
    },
});
