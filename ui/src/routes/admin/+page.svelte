<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { createRecAdminApi, type RecStats } from '$lib/api/client';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();
	const recs = $derived(data.me?.recs ?? []);

	let stats = $state<Record<string, RecStats>>({});

	onMount(async () => {
		// One community is the common case; going straight there saves a click
		// that only ever has one answer.
		if (recs.length === 1) {
			await goto(`/admin/${recs[0].slug}`, { replaceState: true });
			return;
		}
		for (const rec of recs) {
			try {
				stats[rec.slug] = await createRecAdminApi(rec.slug).stats();
			} catch {
				// A community whose counts cannot be read is still listed: the
				// picker's job is to say what exists, not to hide what is failing.
			}
		}
	});

	function pending(slug: string): number {
		const s = stats[slug];
		if (!s) return 0;
		return (s.by_status.submitted ?? 0) + (s.by_status.under_review ?? 0);
	}
</script>

<section>
	<h1>Comunita'</h1>
	<p class="lead">Le comunita' su cui hai permessi.</p>

	<div class="grid">
		{#each recs as rec}
			<a class="card" href="/admin/{rec.slug}">
				<h2>{rec.name}</h2>
				<span class="slug">{rec.slug}</span>
				{#if rec.organization}
					<span class="org">org: {rec.organization}</span>
				{:else}
					<span class="org muted">nessuna organizzazione</span>
				{/if}

				{#if stats[rec.slug]}
					<div class="counts">
						<strong>{pending(rec.slug)}</strong>
						<span>da lavorare</span>
						{#if stats[rec.slug].submissions_with_failed_steps > 0}
							<span class="failed">
								{stats[rec.slug].submissions_with_failed_steps} con abilitazione fallita
							</span>
						{/if}
					</div>
				{/if}
			</a>
		{/each}
	</div>
</section>

<style>
	h1 {
		font-size: 1.5rem;
		margin-bottom: 0.25rem;
	}

	.lead {
		color: var(--celine-text-secondary);
		margin-bottom: 1.5rem;
	}

	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 1rem;
	}

	.card {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 1rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		text-decoration: none;
		color: inherit;
	}

	.card:hover {
		border-color: var(--celine-primary);
	}

	h2 {
		font-size: 1rem;
		margin: 0;
	}

	.slug,
	.org {
		font-size: 0.8125rem;
		color: var(--celine-text-secondary);
	}

	.muted {
		font-style: italic;
	}

	.counts {
		margin-top: 0.75rem;
		display: flex;
		align-items: baseline;
		gap: 0.375rem;
		flex-wrap: wrap;
	}

	.counts strong {
		font-size: 1.5rem;
		color: var(--celine-primary);
	}

	.counts span {
		font-size: 0.8125rem;
		color: var(--celine-text-secondary);
	}

	.failed {
		width: 100%;
		color: var(--celine-error, #b91c1c) !important;
		font-weight: 600;
	}
</style>
