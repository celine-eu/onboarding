import { defineConfig } from '@playwright/test';

export default defineConfig({
	webServer: {
		command: 'pnpm dev',
		port: 5173,
		reuseExistingServer: true,
	},
	testDir: 'tests',
	testMatch: '**/*.spec.ts',
	use: {
		baseURL: 'http://localhost:5173',
		viewport: { width: 375, height: 812 },
	},
});
