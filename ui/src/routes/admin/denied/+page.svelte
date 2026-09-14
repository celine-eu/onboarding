<script lang="ts">
	import { page } from '$app/state';
	import { t } from '$lib/i18n';

	const reason = $derived(page.url.searchParams.get('reason'));
</script>

<svelte:head><title>{$t('admin.denied.page_title')}</title></svelte:head>

<section class="denied">
	<h1>{$t('admin.denied.title')}</h1>
	<p>{$t('admin.denied.body')}</p>
	{#if reason}
		<p class="reason">{reason}</p>
	{/if}
	<p class="hint">
		{$t('admin.denied.hint')}
		<code>admins</code>, <code>managers</code>, <code>editors</code>, <code>viewers</code>
	</p>
	<p><a href="/oauth2/sign_out">{$t('admin.denied.switch_account')}</a></p>
</section>

<style>
	.denied {
		max-width: 34rem;
		margin: 4rem auto;
		text-align: center;
	}

	h1 {
		font-size: 1.5rem;
		margin-bottom: 1rem;
	}

	p {
		color: var(--celine-text-secondary);
		margin-bottom: 1rem;
		line-height: 1.6;
	}

	.reason {
		padding: 0.75rem 1rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-size: 0.875rem;
	}

	.hint code {
		background: var(--celine-bg-elevated);
		padding: 0.125rem 0.25rem;
		border-radius: 3px;
	}
</style>
