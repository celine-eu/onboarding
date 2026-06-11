<script lang="ts">
	import { t } from '$lib/i18n';
	import type { SiteConfig } from '$lib/api/client';

	let { data, children } = $props();
	let config: SiteConfig = $derived(data.config);

	$effect(() => {
		applyBranding(config);
	});

	function applyBranding(cfg: SiteConfig) {
		const color = cfg.branding?.primary_color;
		if (!color) return;

		const r = parseInt(color.slice(1, 3), 16);
		const g = parseInt(color.slice(3, 5), 16);
		const b = parseInt(color.slice(5, 7), 16);

		const root = document.documentElement;
		root.style.setProperty('--celine-primary', color);
		root.style.setProperty('--celine-primary-rgb', `${r}, ${g}, ${b}`);
		root.style.setProperty('--celine-primary-hover', darken(r, g, b, 0.15));
		root.style.setProperty('--celine-primary-light', `rgba(${r}, ${g}, ${b}, 0.1)`);
		root.style.setProperty('--celine-primary-bg', `rgba(${r}, ${g}, ${b}, 0.06)`);
		root.style.setProperty('--celine-primary-text', luminance(r, g, b) > 0.4 ? '#1a1a2e' : '#ffffff');
	}

	function darken(r: number, g: number, b: number, amount: number): string {
		const f = 1 - amount;
		return `rgb(${Math.round(r * f)}, ${Math.round(g * f)}, ${Math.round(b * f)})`;
	}

	function luminance(r: number, g: number, b: number): number {
		const [rs, gs, bs] = [r, g, b].map((c) => {
			const s = c / 255;
			return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
		});
		return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
	}
</script>

<svelte:head>
	<title>{config?.name ?? 'CER'}: {$t('common.onboarding_title')}</title>
</svelte:head>

{@render children()}
