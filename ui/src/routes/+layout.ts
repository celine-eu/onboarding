import { browser } from '$app/environment';
import { loadTranslations, defaultLocale, storedLocale } from '$lib/i18n';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async ({ url }) => {
	const { pathname } = url;
	// The server cannot see the browser's choice, so it renders the default and the
	// browser's own run of this load swaps the text in. Reading the stored choice on
	// every run is also what keeps a client-side navigation from resetting it.
	const lang = browser ? storedLocale() : defaultLocale;

	await loadTranslations(lang, pathname);

	return { lang };
};
