<script lang="ts">
	import { onMount } from 'svelte';
	import { type RegistryDrift } from '$lib/api/client';
	import { t } from '$lib/i18n';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();

	// The drift check reads the registry and writes nothing. Syncing is a platform
	// admin's act, from the CLI or the API, so the console only shows the state.
	let drift = $state<RegistryDrift | null>(null);
	let errorMsg = $state('');
	let loading = $state(true);

	onMount(async () => {
		try {
			drift = await data.api.registryDrift();
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	});

	function stateLabel(state: string): string {
		return $t(`admin.areas.state.${state}`, { default: state });
	}
</script>

<h1>{$t('admin.areas.title')}</h1>
<p class="lead">{$t('admin.areas.lead')}</p>

{#if errorMsg}
	<p class="message error">{$t('admin.areas.failed', { message: errorMsg })}</p>
{:else if loading}
	<p class="muted">{$t('admin.loading')}</p>
{:else if drift}
	{#if drift.status === 'not_synced'}
		<p class="message">{$t('admin.areas.not_synced')}</p>
	{:else}
		<p class="muted">{$t('admin.areas.community', { community: drift.community ?? '' })}</p>
		<p class="message" class:ok={drift.status === 'matches'} class:warn={drift.status === 'drift'}>
			{drift.status === 'matches' ? $t('admin.areas.matches') : $t('admin.areas.drift')}
		</p>
		<div class="table-wrap">
			<table>
				<thead>
					<tr>
						<th>{$t('admin.areas.col_area')}</th>
						<th>{$t('admin.areas.col_boundary')}</th>
						<th>{$t('admin.areas.col_state')}</th>
					</tr>
				</thead>
				<tbody>
					{#each drift.areas as area}
						<tr>
							<td><strong>{area.key}</strong></td>
							<td class="mono">{area.boundary_id ?? '—'}</td>
							<td class:drifted={area.state !== 'matches'}>
								{stateLabel(area.state)}
								{#if area.held_by.length > 0}
									<span class="muted held">
										{$t('admin.areas.held_by', { areas: area.held_by.join(', ') })}
									</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
{/if}

<style>
	h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
	.lead { color: var(--celine-text-secondary); margin-bottom: 1rem; }
	.table-wrap {
		overflow-x: auto; border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm); background: var(--celine-bg-elevated);
	}
	table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
	th {
		text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--celine-border);
		font-size: 0.75rem; text-transform: uppercase; color: var(--celine-text-secondary);
	}
	td { padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--celine-border); vertical-align: top; }
	.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
	.muted { color: var(--celine-text-secondary); }
	.held { display: block; font-size: 0.6875rem; }
	.drifted { color: var(--celine-warning-text); font-weight: 600; }
	.message {
		padding: 0.75rem 1rem; border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-hover); margin-bottom: 1rem;
	}
	.message.ok { background: var(--celine-success-bg); color: var(--celine-success-text); }
	.message.warn { background: var(--celine-warning-bg); color: var(--celine-warning-text); }
	.message.error { background: #fee2e2; color: #991b1b; }
</style>
