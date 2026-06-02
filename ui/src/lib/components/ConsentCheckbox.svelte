<script lang="ts">
	interface Props {
		label: string;
		checked?: boolean;
		required?: boolean;
		documentUrl?: string;
		documentLabel?: string;
		onchange?: (checked: boolean) => void;
	}

	let {
		label,
		checked = $bindable(false),
		required = false,
		documentUrl,
		documentLabel,
		onchange
	}: Props = $props();
</script>

<div class="consent-row">
	<label class="consent">
		<input
			type="checkbox"
			class="consent-input"
			bind:checked
			{required}
			onchange={() => onchange?.(checked)}
		/>
		<span class="consent-label">{label}</span>
	</label>
	{#if documentUrl}
		<a href={documentUrl} target="_blank" rel="noopener" class="consent-doc-link">
			{documentLabel ?? 'PDF'}
			<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
				<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
				<polyline points="15 3 21 3 21 9"/>
				<line x1="10" y1="14" x2="21" y2="3"/>
			</svg>
		</a>
	{/if}
</div>

<style>
	.consent-row {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--celine-space-sm);
		padding: var(--celine-space-sm) 0;
	}

	.consent {
		display: flex;
		align-items: flex-start;
		gap: var(--celine-space-sm);
		cursor: pointer;
		flex: 1;
	}

	.consent-input {
		width: 1.125rem;
		height: 1.125rem;
		margin-top: 0.125rem;
		accent-color: var(--celine-primary);
		cursor: pointer;
		flex-shrink: 0;
	}

	.consent-label {
		font-size: 0.9375rem;
		color: var(--celine-text);
		line-height: 1.4;
	}

	.consent-doc-link {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--celine-primary);
		text-decoration: none;
		white-space: nowrap;
		padding: 0.25rem 0.5rem;
		border-radius: var(--celine-radius-sm);
		transition: background var(--celine-transition-fast);
		flex-shrink: 0;
	}

	.consent-doc-link:hover {
		background: var(--celine-primary-light);
	}
</style>
