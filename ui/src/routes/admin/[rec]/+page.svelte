<script lang="ts">
	import { onMount } from 'svelte';
	import { AdminDeniedError, type AdminSubmission, type RecStats } from '$lib/api/client';
	import { intlLocale, locale, t } from '$lib/i18n';
	import type { PageData } from './$types';

	const { data }: { data: PageData } = $props();
	const api = $derived(data.api);

	const STATUSES = ['draft', 'submitted', 'under_review', 'approved', 'rejected'];

	function statusLabel(status: string): string {
		return $t(`admin.status.${status}`, { default: status });
	}

	const PAGE_SIZE = 25;

	let submissions = $state<AdminSubmission[]>([]);
	let total = $state(0);
	let skip = $state(0);
	let statusFilter = $state('');
	let refFilter = $state('');
	let stats = $state<RecStats | null>(null);
	let loading = $state(false);
	let errorMsg = $state('');

	async function load() {
		loading = true;
		errorMsg = '';
		try {
			const page = await api.listSubmissions({
				status: statusFilter || undefined,
				ref: refFilter.trim() || undefined,
				skip,
				limit: PAGE_SIZE
			});
			submissions = page.submissions;
			total = page.total;
		} catch (e) {
			errorMsg = e instanceof AdminDeniedError ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function applyFilters() {
		// Any filter change resets to the first page — otherwise page 4 of the old
		// filter silently becomes an empty page 4 of the new one.
		skip = 0;
		await load();
	}

	onMount(async () => {
		await load();
		try {
			stats = await api.stats();
		} catch {
			// Counts are a convenience; the queue itself is the page.
		}
	});

	function fullName(s: AdminSubmission): string {
		return [s.first_name, s.last_name].filter(Boolean).join(' ') || '—';
	}

	function formatDate(value?: string | null): string {
		if (!value) return '—';
		const date = new Date(value);
		return Number.isNaN(date.getTime())
			? '—'
			: new Intl.DateTimeFormat(intlLocale($locale), {
					day: '2-digit',
					month: '2-digit',
					year: 'numeric',
					hour: '2-digit',
					minute: '2-digit'
				}).format(date);
	}
</script>

<section>
	<header class="head">
		<div>
			<h1>{data.access.name}</h1>
			<p class="lead">{$t('admin.queue.count', { count: total })}</p>
		</div>
		{#if stats}
			<div class="chips">
				{#each Object.entries(stats.by_status) as [status, count]}
					<button
						class="chip"
						class:active={statusFilter === status}
						onclick={() => {
							statusFilter = statusFilter === status ? '' : status;
							void applyFilters();
						}}
					>
						{statusLabel(status)}
						<strong>{count}</strong>
					</button>
				{/each}
				{#if stats.submissions_with_failed_steps > 0}
					<span class="chip failed" title={$t('admin.queue.failed_title')}>
						{$t('admin.queue.failed_chip')} <strong>{stats.submissions_with_failed_steps}</strong>
					</span>
				{/if}
			</div>
		{/if}
	</header>

	<form
		class="toolbar"
		onsubmit={(e) => {
			e.preventDefault();
			void applyFilters();
		}}
	>
		<label>
			<span>{$t('admin.queue.ref')}</span>
			<input bind:value={refFilter} placeholder="20260730-…" title={$t('admin.queue.ref_hint')} />
		</label>
		<label>
			<span>{$t('admin.queue.status')}</span>
			<select bind:value={statusFilter}>
				<option value="">{$t('admin.queue.all')}</option>
				{#each STATUSES as status}
					<option value={status}>{statusLabel(status)}</option>
				{/each}
			</select>
		</label>
		<button type="submit" class="primary" disabled={loading}>
			{loading ? $t('admin.loading') : $t('admin.queue.filter')}
		</button>
	</form>

	{#if errorMsg}
		<p class="message error">{errorMsg}</p>
	{/if}

	{#if submissions.length === 0 && !loading}
		<p class="empty">{$t('admin.queue.empty')}</p>
	{:else}
		<div class="table-wrap">
			<table>
				<thead>
					<tr>
						<th>{$t('admin.queue.col_ref')}</th>
						<th>{$t('admin.queue.col_applicant')}</th>
						<th>{$t('admin.queue.col_contacts')}</th>
						<th>{$t('admin.queue.col_status')}</th>
						<th>{$t('admin.queue.col_updated')}</th>
					</tr>
				</thead>
				<tbody>
					{#each submissions as submission}
						<tr>
							<td>
								<a href="/admin/{data.rec}/submissions/{submission.id}">{submission.ref}</a>
							</td>
							<td>
								<strong>{fullName(submission)}</strong>
								<span class="muted">{submission.fiscal_code ?? '—'}</span>
							</td>
							<td>
								<span>{submission.email ?? '—'}</span>
								<span class="muted">{submission.phone ?? '—'}</span>
							</td>
							<td>
								<span class="status" data-status={submission.status}>
									{statusLabel(submission.status)}
								</span>
							</td>
							<td>{formatDate(submission.updated_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		{#if total > PAGE_SIZE}
			<nav class="pager">
				<button
					disabled={skip === 0 || loading}
					onclick={() => {
						skip = Math.max(0, skip - PAGE_SIZE);
						void load();
					}}>{$t('admin.queue.previous')}</button
				>
				<span>
					{$t('admin.queue.range', {
						from: skip + 1,
						to: Math.min(skip + PAGE_SIZE, total),
						total
					})}
				</span>
				<button
					disabled={skip + PAGE_SIZE >= total || loading}
					onclick={() => {
						skip += PAGE_SIZE;
						void load();
					}}>{$t('admin.queue.next')}</button
				>
			</nav>
		{/if}
	{/if}
</section>

<style>
	.head {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 1rem;
	}

	h1 {
		font-size: 1.5rem;
		margin: 0;
	}

	.lead {
		color: var(--celine-text-secondary);
	}

	.chips {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.chip {
		display: inline-flex;
		align-items: baseline;
		gap: 0.375rem;
		padding: 0.375rem 0.625rem;
		border: 1px solid var(--celine-border);
		border-radius: 999px;
		background: var(--celine-bg-elevated);
		color: var(--celine-text-secondary);
		font-size: 0.8125rem;
		cursor: pointer;
	}

	.chip.active {
		border-color: var(--celine-primary);
		color: var(--celine-primary);
	}

	.chip.failed {
		border-color: var(--celine-error, #b91c1c);
		color: var(--celine-error, #b91c1c);
		cursor: default;
	}

	.toolbar {
		display: flex;
		gap: 0.75rem;
		align-items: flex-end;
		flex-wrap: wrap;
		margin-bottom: 1rem;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	label span {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--celine-text-secondary);
	}

	input,
	select {
		min-height: 2.25rem;
		padding: 0 0.5rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-size: 0.875rem;
	}

	button {
		min-height: 2.25rem;
		padding: 0 0.875rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
	}

	button.primary {
		background: var(--celine-primary);
		border-color: var(--celine-primary);
		color: #fff;
	}

	button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.table-wrap {
		overflow-x: auto;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.875rem;
	}

	th {
		text-align: left;
		padding: 0.625rem 0.75rem;
		border-bottom: 1px solid var(--celine-border);
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--celine-text-secondary);
	}

	td {
		padding: 0.625rem 0.75rem;
		border-bottom: 1px solid var(--celine-border);
		vertical-align: top;
	}

	td span {
		display: block;
	}

	.muted {
		color: var(--celine-text-secondary);
		font-size: 0.8125rem;
	}

	.status {
		display: inline-block;
		padding: 0.125rem 0.5rem;
		border-radius: 999px;
		font-size: 0.75rem;
		font-weight: 600;
		background: var(--celine-bg);
	}

	.status[data-status='approved'] {
		background: #dcfce7;
		color: #166534;
	}

	.status[data-status='rejected'] {
		background: #fee2e2;
		color: #991b1b;
	}

	.status[data-status='under_review'] {
		background: #fef3c7;
		color: #92400e;
	}

	.pager {
		display: flex;
		align-items: center;
		gap: 1rem;
		margin-top: 1rem;
		font-size: 0.875rem;
		color: var(--celine-text-secondary);
	}

	.message.error {
		padding: 0.75rem 1rem;
		border-radius: var(--celine-radius-sm);
		background: #fee2e2;
		color: #991b1b;
		margin-bottom: 1rem;
	}

	.empty {
		padding: 3rem;
		text-align: center;
		color: var(--celine-text-secondary);
	}
</style>
