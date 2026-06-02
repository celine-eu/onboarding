import { loadTranslations, defaultLocale } from '$lib/i18n';
import type { LayoutLoad } from './$types';

export const load: LayoutLoad = async ({ url }) => {
	const { pathname } = url;
	const lang = defaultLocale;

	await loadTranslations(lang, pathname);

	return { lang };
};
