<script lang="ts">
	interface Props {
		label: string;
		checked?: boolean;
		required?: boolean;
		documentUrl?: string;
		documentLabel?: string;
		/** Articles of the document the label names ("art. 5"): each links to its heading. */
		articles?: number[];
		onchange?: (checked: boolean) => void;
	}

	let {
		label,
		checked = $bindable(false),
		required = false,
		documentUrl,
		documentLabel,
		articles = [],
		onchange
	}: Props = $props();

	// The label in parts, "art. N" of a listed article as a link to the legal host's anchor
	// for its heading (`#sec-N`). The words are the text's own; only the links are added.
	let parts = $derived.by(() => {
		if (!documentUrl || articles.length === 0) return [{ text: label, href: null }];
		const base = documentUrl.split('#')[0];
		const out: { text: string; href: string | null }[] = [];
		let last = 0;
		for (const m of label.matchAll(/\bart\. (\d+)\b/gi)) {
			if (!articles.includes(Number(m[1]))) continue;
			out.push({ text: label.slice(last, m.index), href: null });
			out.push({ text: m[0], href: `${base}#sec-${m[1]}` });
			last = m.index + m[0].length;
		}
		out.push({ text: label.slice(last), href: null });
		return out;
	});
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
		<span class="consent-label"
			>{#each parts as part}{#if part.href}<a
						href={part.href}
						target="_blank"
						rel="noopener">{part.text}</a
					>{:else}{part.text}{/if}{/each}</span
		>
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

	.consent-label a {
		color: var(--celine-primary);
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
