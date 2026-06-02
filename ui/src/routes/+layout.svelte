<script lang="ts">
	import '../app.css';
	import { locale, locales, t } from '$lib/i18n';
	import { api, type SiteConfig } from '$lib/api/client';

	let { children } = $props();
	let config = $state<SiteConfig | null>(null);

	$effect(() => {
		api.getConfig().then((c) => {
			config = c;
			applyBranding(c);
		}).catch(() => {});
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

	function switchLocale(lang: string) {
		locale.set(lang);
	}
</script>

<svelte:head>
	<title>{config?.name ?? 'CER'}: {$t('common.onboarding_title')}</title>
</svelte:head>

<div class="app">
	<header class="header">
		<div class="header-inner">
			<h1 class="header-brand">{config?.name ?? 'CER'}: {$t('common.onboarding_title')}</h1>
			<div class="locale-switcher">
				{#each $locales as lang}
					<button
						class="locale-btn"
						class:active={$locale === lang}
						onclick={() => switchLocale(lang)}
					>
						{lang.toUpperCase()}
					</button>
				{/each}
			</div>
		</div>
	</header>

	<main class="content-wrap">
		{@render children()}
	</main>
</div>

<style>
	.app {
		min-height: 100vh;
	}

	.header {
		position: sticky;
		top: 0;
		z-index: 20;
		border-bottom: 1px solid var(--celine-border);
		background: var(--celine-bg-elevated);
	}

	.header-inner {
		max-width: 900px;
		margin: 0 auto;
		padding: var(--celine-space-sm) var(--celine-space-md);
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.header-brand {
		font-family: var(--celine-font-display);
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--celine-primary);
		margin: 0;
	}

	.locale-switcher {
		display: flex;
		gap: 2px;
	}

	.locale-btn {
		padding: var(--celine-space-xs) var(--celine-space-sm);
		border: none;
		border-radius: var(--celine-radius-sm);
		background: transparent;
		color: var(--celine-text-tertiary);
		font-size: 0.8125rem;
		font-weight: 500;
		cursor: pointer;
		transition: all var(--celine-transition-fast);
	}

	.locale-btn:hover {
		background: var(--celine-bg-hover);
	}

	.locale-btn.active {
		background: var(--celine-primary-light);
		color: var(--celine-primary);
		font-weight: 600;
	}

	.content-wrap {
		max-width: 900px;
		margin: 0 auto;
		padding: var(--celine-space-lg) var(--celine-space-md);
	}
</style>
