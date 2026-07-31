import { defineConfig } from '@playwright/test';

// When PLAYWRIGHT_BASE_URL is set, something else (scripts/e2e.sh) is already
// serving a build against a real backend, and starting `pnpm dev` here would
// point the suite at an app with no API behind it.
const external = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
	...(external
		? {}
		: {
				webServer: {
					command: 'pnpm dev',
					port: 5173,
					reuseExistingServer: true
				}
			}),
	testDir: 'tests',
	testMatch: '**/*.spec.ts',
	use: {
		baseURL: external ?? 'http://localhost:5173',
		viewport: { width: 1280, height: 900 }
	}
});
