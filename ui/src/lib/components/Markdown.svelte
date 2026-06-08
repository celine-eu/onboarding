<script lang="ts">
	import DOMPurify from 'isomorphic-dompurify';
	import { marked } from 'marked';

	interface Props {
		content: string;
	}

	let { content }: Props = $props();

	function autolink(html: string): string {
		return html.replace(/(<[^>]+>)|([^<]+)/g, (_, tag, text) => {
			if (tag) return tag;
			if (!text) return '';
			text = text.replace(
				/\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z]{2,})\b/gi,
				'<a href="mailto:$1">$1</a>'
			);
			text = text.replace(
				/(https?:\/\/[^\s<)"]+)/gi,
				'<a href="$1" target="_blank" rel="noopener">$1</a>'
			);
			return text;
		});
	}

	let rendered = $derived(
		DOMPurify.sanitize(
			autolink(marked.parse(content, { async: false, gfm: true, breaks: true }) as string),
			{ ADD_ATTR: ['target'] }
		)
	);
</script>

<div class="md-content">
	{@html rendered}
</div>


<style>
	.md-content {
		font-size: 0.9375rem;
		line-height: 1.6;
		color: var(--celine-text-secondary);
	}

	.md-content :global(p) {
		margin: 0 0 var(--celine-space-sm);
	}

	.md-content :global(p:last-child) {
		margin-bottom: 0;
	}

	.md-content :global(strong) {
		color: var(--celine-text);
		font-weight: 600;
	}

	.md-content :global(a) {
		color: var(--celine-primary);
		text-decoration: underline;
	}

	.md-content :global(a:hover) {
		color: var(--celine-primary-hover);
	}

	.md-content :global(ul),
	.md-content :global(ol) {
		margin: 0 0 var(--celine-space-sm);
		padding-left: var(--celine-space-lg);
	}
</style>
