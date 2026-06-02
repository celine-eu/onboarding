<script lang="ts">
	import { t } from '$lib/i18n';
	import { api, type SiteConfig } from '$lib/api/client';

	let config = $state<SiteConfig | null>(null);

	$effect(() => {
		api.getConfig().then((c) => (config = c)).catch(() => {});
	});
</script>

<div class="hero">
	<div class="hero-icon">
		{#if config?.branding?.logo}
			<img src="/api/assets/{config.branding.logo}" alt="" width="48" height="48" />
		{:else}
			<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M13 10V3L4 14h7v7l9-11h-7z" />
			</svg>
		{/if}
	</div>

	<h2 class="hero-title">{config?.name ?? $t('common.app_title')}</h2>
	<p class="hero-subtitle">{config?.content?.welcome ?? $t('common.app_subtitle')}</p>

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
