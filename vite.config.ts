// vite.config.ts

import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { buildVersionLabel } from './src/lib/build-version';
import { vanillaExtractPlugin } from '@vanilla-extract/vite-plugin';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, searchForWorkspaceRoot } from 'vite';
import { configDefaults } from 'vitest/config';

// closed-source modules (AI Agent, future tiered features) live in the
// private repo, checked out into ./modules on desktop builds; open-source
// builds resolve '@modules' to the empty stub manifest
const modulesDir = path.resolve(__dirname, './modules/index.ts');
const modulesTarget = fs.existsSync(modulesDir)
    ? modulesDir
    : path.resolve(__dirname, './src/modules-stub/index.ts');
// In the split public/private worktree, ./modules is a symlink into the
// private checkout. Vite resolves Worker URLs to that real path, so explicitly
// allow the resolved modules directory without hard-coding either repository.
const modulesAllowDir = path.dirname(fs.realpathSync(modulesTarget));
const pkg = JSON.parse(
    fs.readFileSync(path.resolve(__dirname, 'package.json'), 'utf8'),
) as { version?: string };

export default defineConfig(({ mode, command }) => {
    const env = loadEnv(mode, process.cwd(), '');
    let revision: string | undefined;
    let dirty = false;
    try {
        revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: __dirname, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
        dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], { cwd: __dirname, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim().length > 0;
    } catch {
        // Source archives remain explicitly dev/unknown, never a stale release.
    }
    const displayVersion = buildVersionLabel({
        command, revision, dirty,
        refType: process.env.GITHUB_REF_TYPE,
        refName: process.env.GITHUB_REF_NAME,
    });
    return {
        base: env.VITE_BASE ?? '/',
        // shioaji app upload flattens nested paths — emit a flat bundle.
        // target: old Intel Macs run older WKWebView (Safari 13–15 era);
        // Vite 8's default (baseline-widely-available ≈ Safari 16) emits
        // syntax those webviews cannot parse → white screen on launch (#4)
        build: {
            assetsDir: '',
            target: ['es2020', 'safari13'],
            rolldownOptions: {
                // agent-approval 獨立視窗頁（docs/design/order-confirm-split.md）
                input: {
                    main: path.resolve(__dirname, 'index.html'),
                    approval: path.resolve(__dirname, 'approval.html'),
                },
                output: {
                    codeSplitting: {
                        groups: [
                            {
                                name: 'agent',
                                test: /[\\/]modules[\\/]agent[\\/]/,
                                priority: 4,
                            },
                            {
                                name: 'backtest',
                                test: /[\\/]modules[\\/]backtest[\\/]/,
                                priority: 3,
                            },
                            {
                                name: 'markdown',
                                test: /node_modules[\\/](?:react-markdown|remark-|rehype-|unified|micromark|mdast|hast)/,
                                priority: 2,
                            },
                            {
                                name: 'vendor',
                                test: /node_modules/,
                                priority: 1,
                                maxSize: 400_000,
                            },
                        ],
                    },
                },
            },
        },
        // react-draggable (react-grid-layout dep) reads process.env at runtime
        define: {
            'process.env': {},
            // feature-flag service client key (publishable) — from .env
            // locally, or the STATSIG_CLIENT_KEY secret in CI builds
            __STATSIG_CLIENT_KEY__: JSON.stringify(
                env.STATSIG_CLIENT_KEY ??
                    process.env.STATSIG_CLIENT_KEY ??
                    '',
            ),
            __SHIOAJI_APP_VERSION__: JSON.stringify(pkg.version ?? ''),
            __SHIOAJI_BUILD_VERSION__: JSON.stringify(displayVersion),
            // bundled server version（repo 根目錄 SHIOAJI_VERSION —
            // 與 CI 下載 sidecar 的同一個來源）— app 開機做版本握手
            __SHIOAJI_SERVER_VERSION__: JSON.stringify(
                fs
                    .readFileSync(
                        path.resolve(__dirname, 'SHIOAJI_VERSION'),
                        'utf8',
                    )
                    .trim(),
            ),
        },
        plugins: [vanillaExtractPlugin(), react()],
        test: {
            // Node can expose a `localStorage` name backed by an unavailable
            // file while running in a sandbox.  Tests need the browser
            // Storage contract, not that host-specific partial object.
            setupFiles: ['./test/vitest-setup.ts'],
            // The desktop overlay is mirrored here for Tauri dev/CI, but its
            // Rust-adjacent Node tests use node:test rather than Vitest.
            exclude: [
                ...configDefaults.exclude,
                'src-tauri/**',
                'plugins/**/test/*.test.mjs',
                'e2e/**',
                'e2e-paper/**',
            ],
        },
        resolve: {
            alias: {
                '@modules': modulesTarget,
                '@': path.resolve(__dirname, './src'),
            },
        },
        server: {
            // honor a harness-assigned port (preview tooling sets PORT);
            // default stays 5173 for tauri dev. Bind loopback only so Vite
            // is never exposed to the LAN.
            host: '127.0.0.1',
            port: Number(process.env.PORT) || 5173,
            fs: {
                allow: [searchForWorkspaceRoot(process.cwd()), modulesAllowDir],
            },
            proxy: {
                // Public Web must use the local Gateway as its only API
                // origin.  The Gateway owns compatibility allowlists and
                // fails closed for every order mutation; it can read from a
                // sidecar internally without exposing that sidecar to the
                // browser. This deliberately has no environment override:
                // a build-time proxy escape hatch would let Web traffic skip
                // the Gateway's compatibility and mutation boundaries.
                '/api': 'http://127.0.0.1:18082',
            },
        },
        // `vite preview` does not inherit `server.proxy`. Keep its browser
        // requests on the same Gateway-only contract as `vite dev`; static
        // production builds use the validated VITE_API_BASE fallback in
        // runtime.ts and therefore cannot silently talk to a sidecar.
        preview: {
            host: '127.0.0.1',
            proxy: {
                '/api': 'http://127.0.0.1:18082',
            },
        },
    };
});
