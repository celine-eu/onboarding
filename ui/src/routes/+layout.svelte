<script lang="ts">
	import '../app.css';
	import { locale, locales, t } from '$lib/i18n';

	let { children } = $props();

	function switchLocale(lang: string) {
		locale.set(lang);
	}
</script>

<svelte:head>
	<title>{$t('common.app_title')}</title>
</svelte:head>

<div class="app">
	<header class="header">
		<div class="header-inner">
			<a href="/" class="header-brand">{$t('common.app_title')}</a>
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
		text-decoration: none;
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
