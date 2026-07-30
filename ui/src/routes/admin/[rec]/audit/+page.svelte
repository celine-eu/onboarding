<script lang="ts">
	import { onMount } from 'svelte';
	import { type AuditEntry } from '$lib/api/client';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();

	let entries = $state<AuditEntry[]>([]);
	let actionFilter = $state('');
	let errorMsg = $state('');
	let loading = $state(true);

	const filtered = $derived(
		actionFilter ? entries.filter((e) => e.action === actionFilter) : entries
	);
	const actions = $derived([...new Set(entries.map((e) => e.action))].sort());

	onMount(async () => {
		try {
			entries = await data.api.auditLogs(200);
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	});

	function formatDate(value: string): string {
		const d = new Date(value);
		return Number.isNaN(d.getTime())
			? value
			: new Intl.DateTimeFormat('it-IT', { dateStyle: 'short', timeStyle: 'medium' }).format(d);
	}

	function actor(entry: AuditEntry): string {
		if (entry.actor_email) return entry.actor_email;
		if (entry.actor_sub) return entry.actor_sub;
		// Rows from before per-operator authorization: nobody can be named.
		return entry.actor_type === 'token' ? 'token condiviso (storico)' : entry.actor_type;
	}
</script>

<h1>Registro attivita'</h1>
<p class="lead">Solo questa comunita'. Le righe piu' recenti per prime.</p>

{#if errorMsg}<p class="message error">{errorMsg}</p>{/if}

<div class="toolbar">
	<label>
		<span>Azione</span>
		<select bind:value={actionFilter}>
			<option value="">Tutte</option>
			{#each actions as action}<option value={action}>{action}</option>{/each}
		</select>
	</label>
</div>

{#if loading}
	<p class="muted">Caricamento…</p>
{:else if filtered.length === 0}
	<p class="muted">Nessuna voce.</p>
{:else}
	<div class="table-wrap">
		<table>
			<thead>
				<tr><th>Quando</th><th>Azione</th><th>Operatore</th><th>Dettaglio</th></tr>
			</thead>
			<tbody>
				{#each filtered as entry}
					<tr>
						<td class="mono">{formatDate(entry.created_at)}</td>
						<td><strong>{entry.action}</strong></td>
						<td>
							{actor(entry)}
							<span class="muted type">{entry.actor_type}</span>
						</td>
						<td class="detail">
							{#if entry.entity_id}
								<a href="/admin/{data.rec}/submissions/{entry.entity_id}">{entry.entity_id.slice(0, 8)}</a>
							{/if}
							{entry.detail ?? ''}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

<style>
	h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
	.lead { color: var(--celine-text-secondary); margin-bottom: 1rem; }
	.toolbar { margin-bottom: 1rem; }
	label { display: flex; flex-direction: column; gap: 0.25rem; max-width: 16rem; }
	label span { font-size: 0.75rem; font-weight: 600; color: var(--celine-text-secondary); }
	select {
		min-height: 2.25rem; padding: 0 0.5rem;
		border: 1px solid var(--celine-border); border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated); color: var(--celine-text); font-size: 0.875rem;
	}
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
	.type { display: block; font-size: 0.6875rem; }
	.detail { word-break: break-word; }
	.message.error {
		padding: 0.75rem 1rem; border-radius: var(--celine-radius-sm);
		background: #fee2e2; color: #991b1b; margin-bottom: 1rem;
	}
</style>
