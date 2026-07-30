<script lang="ts">
	import { t } from '$lib/i18n';
	import { globalApi, type RecSummary, type RecMatch } from '$lib/api/client';

	let allRecs = $state<RecSummary[]>([]);
	let matchedRecs = $state<RecMatch[]>([]);
	let address = $state('');
	let searching = $state(false);
	let searched = $state(false);
	let showAll = $state(false);
	let errorMsg = $state('');

	$effect(() => {
		globalApi.listRecs().then((r) => (allRecs = r)).catch(() => {});
	});

	async function findRecs() {
		if (!address.trim()) return;
		searching = true;
		searched = false;
		errorMsg = '';
		matchedRecs = [];
		try {
			matchedRecs = await globalApi.findRecsByAddress(address);
			searched = true;
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : 'Search failed';
		} finally {
			searching = false;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') findRecs();
	}

	let displayRecs = $derived(showAll ? allRecs : matchedRecs);
</script>

<div class="finder">
	<div class="finder-hero">
		<div class="finder-icon">
			<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M13 10V3L4 14h7v7l9-11h-7z" />
			</svg>
		</div>
		<h2 class="finder-title">{$t('common.app_title')}</h2>
		<p class="finder-subtitle">{$t('common.app_subtitle')}</p>
	</div>

	<div class="search-section">
		<label class="search-label" for="address">{$t('common.find_your_rec')}</label>
		<div class="search-row">
			<input
				id="address"
				class="search-input"
				type="text"
				bind:value={address}
				onkeydown={handleKeydown}
				placeholder={$t('common.address_placeholder')}
			/>
			<button
				class="btn btn-primary"
				disabled={!address.trim() || searching}
				onclick={findRecs}
			>
				{searching ? $t('common.loading') : $t('common.search')}
			</button>
		</div>
	</div>

	{#if errorMsg}
		<div class="error-banner">{errorMsg}</div>
	{/if}

	{#if searched && matchedRecs.length === 0 && !showAll}
		<div class="no-results">
			<p>{$t('common.no_rec_found')}</p>
		</div>
	{/if}

	{#if (searched && matchedRecs.length > 0) || showAll}
		<div class="rec-grid">
			{#each displayRecs as rec (rec.slug)}
				<a href="/{rec.slug}/" class="rec-card" style:--card-color={rec.branding?.primary_color || 'var(--celine-primary)'}>
					<div class="rec-card-accent"></div>
					<div class="rec-card-body">
						<h3 class="rec-card-name">{rec.name}</h3>
						<span class="rec-card-slug">{rec.slug}</span>
					</div>
					<div class="rec-card-arrow">
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
							<path d="M5 12h14M12 5l7 7-7 7"/>
						</svg>
					</div>
				</a>
			{/each}
		</div>
	{/if}

	{#if !showAll}
		<button class="show-all-btn" onclick={() => (showAll = true)}>
			{$t('common.show_all_recs')} ({allRecs.length})
		</button>
	{/if}
</div>

<style>
	.finder {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-xl);
	}

	.finder-hero {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--celine-space-md);
		padding: var(--celine-space-xl) 0;
		text-align: center;
	}

	.finder-icon {
		background: var(--celine-primary-light);
		color: var(--celine-primary);
		border-radius: var(--celine-radius-full);
		padding: var(--celine-space-md);
		display: flex;
	}

	.finder-title {
		font-family: var(--celine-font-display);
		font-size: 1.75rem;
		font-weight: 700;
		color: var(--celine-text);
		margin: 0;
	}

	.finder-subtitle {
		color: var(--celine-text-secondary);
		max-width: 28rem;
		margin: 0;
	}

	.search-section {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-sm);
	}

	.search-label {
		font-weight: 600;
		font-size: 0.9375rem;
		color: var(--celine-text);
	}

	.search-row {
		display: flex;
		gap: var(--celine-space-sm);
	}

	.search-input {
		flex: 1;
		padding: 0.625rem 0.875rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-md);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-family: var(--celine-font-body);
		font-size: 0.9375rem;
	}

	.search-input:focus {
		outline: 2px solid var(--celine-primary);
		outline-offset: 2px;
		border-color: var(--celine-primary);
	}

	.rec-grid {
		display: flex;
		flex-direction: column;
		gap: var(--celine-space-sm);
	}

	.rec-card {
		display: flex;
		align-items: center;
		background: var(--celine-bg-elevated);
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-lg);
		overflow: hidden;
		text-decoration: none;
		transition: box-shadow var(--celine-transition-fast), border-color var(--celine-transition-fast);
	}

	.rec-card:hover {
		box-shadow: var(--celine-shadow-md);
		border-color: var(--card-color);
	}

	.rec-card-accent {
		width: 6px;
		align-self: stretch;
		background: var(--card-color);
		flex-shrink: 0;
	}

	.rec-card-body {
		flex: 1;
		padding: var(--celine-space-md) var(--celine-space-lg);
	}

	.rec-card-name {
		font-family: var(--celine-font-display);
		font-size: 1.0625rem;
		font-weight: 600;
		color: var(--celine-text);
		margin: 0;
	}

	.rec-card-slug {
		font-size: 0.8125rem;
		color: var(--celine-text-tertiary);
	}

	.rec-card-arrow {
		padding: 0 var(--celine-space-lg);
		color: var(--celine-text-tertiary);
	}

	.rec-card:hover .rec-card-arrow {
		color: var(--card-color);
	}

	.no-results {
		text-align: center;
		padding: var(--celine-space-lg);
		color: var(--celine-text-secondary);
	}

	.show-all-btn {
		display: block;
		margin: 0 auto;
		padding: var(--celine-space-sm) var(--celine-space-lg);
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-md);
		background: transparent;
		color: var(--celine-text-secondary);
		font-size: 0.875rem;
		cursor: pointer;
		transition: all var(--celine-transition-fast);
	}

	.show-all-btn:hover {
		background: var(--celine-bg-hover);
		border-color: var(--celine-border-strong);
	}

	.error-banner {
		background: var(--celine-danger-bg);
		color: var(--celine-danger-text);
		padding: var(--celine-space-sm) var(--celine-space-md);
		border-radius: var(--celine-radius-md);
		font-size: 0.875rem;
	}

	.btn {
		padding: 0.5rem 1rem;
		border-radius: var(--celine-radius-md);
		font-size: 0.9375rem;
		font-weight: 500;
		cursor: pointer;
		border: none;
		transition: all var(--celine-transition-fast);
		white-space: nowrap;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-primary {
		background: var(--celine-primary);
		color: var(--celine-primary-text);
	}

	.btn-primary:hover:not(:disabled) {
		background: var(--celine-primary-hover);
	}
</style>
