<script lang="ts">
	const { children }: { children: import('svelte').Snippet } = $props();
</script>

{@render children()}
