import { test, expect } from '@playwright/test';

test.describe('Onboarding wizard', () => {
	test('landing page shows start button', async ({ page }) => {
		await page.goto('/');
		await expect(page.getByText('CER Onboarding')).toBeVisible();
		await expect(page.getByRole('link', { name: /inizia|start/i })).toBeVisible();
	});

	test('navigates to onboarding page', async ({ page }) => {
		await page.goto('/');
		await page.getByRole('link', { name: /inizia|start/i }).click();
		await expect(page).toHaveURL('/onboarding');
		await expect(page.getByText(/adesione|membership/i)).toBeVisible();
	});

	test('wizard step navigation works', async ({ page }) => {
		await page.goto('/onboarding');

		// Step 1: fill personal data
		await page.getByLabel(/nome/i).first().fill('Mario');
		await page.getByLabel(/cognome/i).fill('Rossi');
		await page.getByLabel(/email/i).fill('mario@example.com');

		// Next button should be enabled
		const nextBtn = page.getByRole('button', { name: /avanti|next/i });
		await expect(nextBtn).toBeEnabled();
		await nextBtn.click();

		// Step 2: utility info
		await expect(page.getByLabel(/codice fiscale|fiscal/i)).toBeVisible();

		// Back button should work
		const backBtn = page.getByRole('button', { name: /indietro|back/i });
		await backBtn.click();

		// Should be back on step 1 with data preserved
		await expect(page.getByLabel(/nome/i).first()).toHaveValue('Mario');
	});

	test('language switcher works', async ({ page }) => {
		await page.goto('/');

		// Default is Italian
		await expect(page.getByText('Inizia adesione')).toBeVisible();

		// Switch to English
		await page.getByRole('button', { name: 'EN' }).click();
		await expect(page.getByText('Start onboarding')).toBeVisible();

		// Switch back to Italian
		await page.getByRole('button', { name: 'IT' }).click();
		await expect(page.getByText('Inizia adesione')).toBeVisible();
	});
});
