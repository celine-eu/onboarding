<script lang="ts">
	import { t } from '$lib/i18n';
	import { api, type SiteConfig } from '$lib/api/client';
	import Markdown from '$lib/components/Markdown.svelte';

	let config = $state<SiteConfig | null>(null);

	$effect(() => {
		api.getConfig().then((c) => (config = c)).catch(() => {});
	});
</script>

<div class="hero">
	{#if config?.branding?.logo}
		<div class="hero-logo-wrap">
			<img class="hero-logo" src="/api/template/{config.branding.logo}" alt="" />
		</div>
	{:else}
		<div class="hero-icon">
			<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M13 10V3L4 14h7v7l9-11h-7z" />
			</svg>
		</div>
	{/if}

	<h2 class="hero-title">{config?.name ?? $t('common.app_title')}</h2>
	{#if config?.content?.welcome}
		<div class="hero-subtitle"><Markdown content={config.content.welcome} /></div>
	{:else}
		<p class="hero-subtitle">{$t('common.app_subtitle')}</p>
	{/if}

	<a href="/onboarding" class="cta-btn">
		{$t('common.start_onboarding')}
	</a>
</div>

<style>
	.hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--celine-space-lg);
		padding: var(--celine-space-2xl) 0;
		text-align: center;
	}

	.hero-icon {
		background: var(--celine-primary-light);
		color: var(--celine-primary);
		border-radius: var(--celine-radius-full);
		padding: var(--celine-space-md);
		display: flex;
	}

	.hero-logo-wrap {
		background: var(--celine-primary);
		border-radius: var(--celine-radius-lg);
		padding: var(--celine-space-md) var(--celine-space-xl);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.hero-logo {
		max-height: 60px;
		max-width: 260px;
		object-fit: contain;
	}

	.hero-title {
		font-family: var(--celine-font-display);
		font-size: 1.75rem;
		font-weight: 700;
		color: var(--celine-text);
		margin: 0;
	}

	.hero-subtitle {
		color: var(--celine-text-secondary);
		max-width: 28rem;
		margin: 0;
	}

	.cta-btn {
		display: inline-block;
		background: var(--celine-primary);
		color: var(--celine-primary-text);
		padding: 0.75rem 1.5rem;
		border-radius: var(--celine-radius-md);
		font-weight: 600;
		font-size: 0.9375rem;
		text-decoration: none;
		box-shadow: var(--celine-shadow-sm);
		transition: background var(--celine-transition-fast);
	}

	.cta-btn:hover {
		background: var(--celine-primary-hover);
	}
</style>
