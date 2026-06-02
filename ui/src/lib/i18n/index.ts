import i18n from 'sveltekit-i18n';
import type { Config } from 'sveltekit-i18n';

const config: Config = {
	loaders: [
		{
			locale: 'it',
			key: 'common',
			loader: async () => (await import('./it/common.json')).default
		},
		{
			locale: 'it',
			key: 'onboarding',
			loader: async () => (await import('./it/onboarding.json')).default
		},
		{
			locale: 'en',
			key: 'common',
			loader: async () => (await import('./en/common.json')).default
		},
		{
			locale: 'en',
			key: 'onboarding',
			loader: async () => (await import('./en/onboarding.json')).default
		}
	]
};

export const defaultLocale = 'it';

export const { t, locale, locales, loading, loadTranslations } = new i18n(config);
