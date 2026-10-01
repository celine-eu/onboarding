<script lang="ts">
	import { onMount } from 'svelte';
	import { type SharedDeliveryPoints } from '$lib/api/client';
	import { t } from '$lib/i18n';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();

	// The registry's report of PODs more than one active member holds. A read:
	// the fix is a revision on the wrong member's application, never here.
	let report = $state<SharedDeliveryPoints | null>(null);
	let revealed = $state(false);
	let errorMsg = $state('');
	let loading = $state(true);

	async function load() {
		loading = true;
		errorMsg = '';
		try {
			report = await data.api.sharedDeliveryPoints(revealed);
		} catch (e) {
			errorMsg = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function toggleReveal() {
		revealed = !revealed;
		void load();
	}
</script>

<h1>{$t('admin.shared_pods.title')}</h1>
<p class="lead">{$t('admin.shared_pods.lead')}</p>
<p class="message warn">{$t('admin.shared_pods.guidance')}</p>

{#if errorMsg}
	<p class="message error">{$t('admin.shared_pods.failed', { message: errorMsg })}</p>
{:else if loading}
	<p class="muted">{$t('admin.loading')}</p>
{:else if report}
	<div class="bar">
		<span class="muted">{$t('admin.shared_pods.community', { community: report.community_key })}</span>
		{#if data.can('submissions.reveal')}
			<button class="secondary small" onclick={toggleReveal}>
				{revealed ? $t('admin.shared_pods.hide') : $t('admin.shared_pods.reveal')}
			</button>
		{:else}
			<span class="muted small">{$t('admin.shared_pods.reveal_forbidden')}</span>
		{/if}
	</div>

	{#if report.items.length === 0}
		<p class="message ok">{$t('admin.shared_pods.none')}</p>
	{:else}
		<div class="table-wrap">
			<table>
				<thead>
					<tr>
						<th>{$t('admin.shared_pods.col_pod')}</th>
						<th>{$t('admin.shared_pods.col_holders')}</th>
						<th>{$t('admin.shared_pods.col_elsewhere')}</th>
					</tr>
				</thead>
				<tbody>
					{#each report.items as item (item.delivery_point + item.holders.map((h) => h.member_key).join())}
						<tr>
							<td class="mono">{item.delivery_point}</td>
							<td>
								<ul class="holders">
									{#each item.holders as holder (holder.member_key)}
										<li>
											<span class="mono">{holder.member_key}</span>
											<span class="mono muted small">{holder.id}</span>
											{#if holder.submission_id}
												<a href="/admin/{data.rec}/submissions/{holder.submission_id}">
													{$t('admin.shared_pods.open')}
												</a>
											{:else}
												<span class="muted small">{$t('admin.shared_pods.not_onboarded')}</span>
											{/if}
										</li>
									{/each}
								</ul>
							</td>
							<td>
								{item.held_elsewhere > 0
									? $t('admin.shared_pods.elsewhere', { count: item.held_elsewhere })
									: $t('admin.shared_pods.elsewhere_none')}
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
	.bar {
		display: flex; justify-content: space-between; align-items: center;
		gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.75rem;
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
	.holders { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
	.holders li { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: baseline; }
	.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
	.muted { color: var(--celine-text-secondary); }
	.small { font-size: 0.75rem; }
	button {
		min-height: 1.875rem; padding: 0 0.875rem; border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm); background: var(--celine-bg-elevated);
		color: var(--celine-text); font-size: 0.8125rem; font-weight: 600; cursor: pointer;
	}
	.message {
		padding: 0.75rem 1rem; border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-hover); margin-bottom: 1rem;
	}
	.message.ok { background: var(--celine-success-bg); color: var(--celine-success-text); }
	.message.warn { background: var(--celine-warning-bg); color: var(--celine-warning-text); }
	.message.error { background: #fee2e2; color: #991b1b; }
</style>
