<script lang="ts">
	import { t } from '$lib/i18n';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();

	let recipient = $state('');
	let offerId = $state('');
	let podRecipient = $state('');
	let busy = $state<string | null>(null);
	let errorMsg = $state('');

	function download(blob: Blob, filename: string) {
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.click();
		URL.revokeObjectURL(url);
	}

	function stamp(): string {
		return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
	}

	async function run(name: string, fn: () => Promise<Blob>, filename: string) {
		busy = name;
		errorMsg = '';
		try {
			download(await fn(), filename);
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		} finally {
			busy = null;
		}
	}
</script>

<h1>{$t('admin.exports.title')}</h1>

{#if errorMsg}<p class="message error">{errorMsg}</p>{/if}

{#if !data.can('export')}
	<p class="muted">{$t('admin.exports.forbidden')}</p>
{:else}
	<section class="panel">
		<h2>{$t('admin.exports.csv_title')}</h2>
		<p class="hint">{$t('admin.exports.csv_hint')}</p>
		<div class="row">
			<label>
				<span>{$t('admin.exports.recipient_optional')}</span>
				<input bind:value={recipient} placeholder={$t('admin.exports.recipient_placeholder')} />
			</label>
			<button
				class="primary"
				disabled={busy !== null}
				onclick={() =>
					run('csv', () => data.api.exportCsv(recipient || undefined), `${data.rec}-${$t('admin.exports.csv_filename')}-${stamp()}.csv`)}
			>
				{busy === 'csv' ? $t('admin.exports.exporting') : $t('admin.exports.csv_download')}
			</button>
		</div>
	</section>

	<section class="panel">
		<h2>{$t('admin.exports.pods_title')}</h2>
		<p class="hint">{$t('admin.exports.pods_hint')}</p>
		<div class="row">
			<label>
				<span>{$t('admin.exports.offer')}</span>
				<input bind:value={offerId} placeholder="household-energy-flexibility" />
			</label>
			<label>
				<span>{$t('admin.exports.recipient')}</span>
				<input bind:value={podRecipient} placeholder={$t('admin.exports.recipient_placeholder')} />
			</label>
			<button
				class="primary"
				disabled={busy !== null || !offerId.trim() || !podRecipient.trim()}
				onclick={() =>
					run('pods', () => data.api.exportPodList(offerId, podRecipient), `${data.rec}-${$t('admin.exports.pods_filename')}-${stamp()}.csv`)}
			>
				{busy === 'pods' ? $t('admin.exports.exporting') : $t('admin.exports.pods_download')}
			</button>
		</div>
	</section>
{/if}

<style>
	h1 { font-size: 1.5rem; margin: 0 0 1rem; }
	h2 { font-size: 0.9375rem; margin: 0 0 0.5rem; }
	.panel {
		border: 1px solid var(--celine-border); border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated); padding: 1rem; margin-bottom: 1rem; max-width: 46rem;
	}
	.hint { font-size: 0.8125rem; color: var(--celine-text-secondary); line-height: 1.6; margin-bottom: 0.75rem; }
	.row { display: flex; gap: 0.75rem; align-items: flex-end; flex-wrap: wrap; }
	label { display: flex; flex-direction: column; gap: 0.25rem; flex: 1; min-width: 12rem; }
	label span { font-size: 0.75rem; font-weight: 600; color: var(--celine-text-secondary); }
	input {
		min-height: 2.25rem; padding: 0 0.5rem;
		border: 1px solid var(--celine-border); border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated); color: var(--celine-text); font-size: 0.875rem;
	}
	button {
		min-height: 2.25rem; padding: 0 0.875rem;
		border: 1px solid var(--celine-border); border-radius: var(--celine-radius-sm);
		font-size: 0.875rem; font-weight: 600; cursor: pointer;
	}
	button.primary { background: var(--celine-primary); border-color: var(--celine-primary); color: #fff; }
	button:disabled { opacity: 0.5; cursor: not-allowed; }
	.muted { color: var(--celine-text-secondary); }
	.message.error {
		padding: 0.75rem 1rem; border-radius: var(--celine-radius-sm);
		background: #fee2e2; color: #991b1b; margin-bottom: 1rem;
	}
</style>
