<script lang="ts">
	import '../../app.css';
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { adminPing } from '$lib/api/client';
	import type { LayoutData } from './$types';

	const { data, children }: { data: LayoutData; children: import('svelte').Snippet } = $props();

	const me = $derived(data.me);
	const currentRec = $derived(page.params.rec ?? null);
	const currentAccess = $derived(me?.recs.find((r) => r.slug === currentRec) ?? null);

	let profileOpen = $state(false);

	function initials(): string {
		const name = me?.name ?? me?.preferred_username ?? me?.email ?? '?';
		return name
			.split(/[\s@.]+/)
			.filter(Boolean)
			.slice(0, 2)
			.map((w) => w[0].toUpperCase())
			.join('');
	}

	// The session cookie can lapse while a queue sits open. Polling /ping means
	// the operator finds out on the next tick rather than when they press Approve.
	onMount(() => {
		const timer = setInterval(() => {
			void adminPing().catch(() => {});
		}, 60_000);
		return () => clearInterval(timer);
	});
</script>

<svelte:head>
	<title>Console operatori{currentAccess ? ` · ${currentAccess.name}` : ''}</title>
</svelte:head>

<div class="console">
	<header class="console-header">
		<a class="brand" href="/admin">Console operatori</a>

		{#if me && currentRec}
			<nav class="nav">
				<a href="/admin/{currentRec}" class:active={page.url.pathname === `/admin/${currentRec}`}>
					Pratiche
				</a>
				<a
					href="/admin/{currentRec}/audit"
					class:active={page.url.pathname.endsWith('/audit')}
				>
					Registro
				</a>
				<a
					href="/admin/{currentRec}/exports"
					class:active={page.url.pathname.endsWith('/exports')}
				>
					Esportazioni
				</a>
			</nav>
		{/if}

		<div class="header-right">
			{#if me && me.recs.length > 1}
				<select
					class="rec-switcher"
					value={currentRec ?? ''}
					onchange={(e) => {
						const slug = (e.currentTarget as HTMLSelectElement).value;
						if (slug) window.location.href = `/admin/${slug}`;
					}}
				>
					<option value="">Scegli comunita'...</option>
					{#each me.recs as rec}
						<option value={rec.slug}>{rec.name}</option>
					{/each}
				</select>
			{/if}

			{#if me}
				<button class="avatar" onclick={() => (profileOpen = !profileOpen)} aria-label="Profilo">
					{initials()}
				</button>
				{#if profileOpen}
					<div class="dropdown" role="menu">
						<div class="dropdown-id">
							<strong>{me.email ?? me.preferred_username ?? me.sub}</strong>
							<span>{me.subject_type}</span>
						</div>
						{#if currentAccess}
							<div class="dropdown-caps">
								<span>Permessi su {currentAccess.name}</span>
								<ul>
									{#each currentAccess.capabilities as capability}
										<li>{capability}</li>
									{/each}
								</ul>
							</div>
						{/if}
						<hr />
						<a href="/oauth2/sign_out" role="menuitem">Esci</a>
					</div>
				{/if}
			{/if}
		</div>
	</header>

	<main class="console-main">
		{@render children()}
	</main>
</div>

<style>
	:global(body) {
		background: var(--celine-bg);
	}

	.console {
		display: flex;
		flex-direction: column;
		min-height: 100dvh;
	}

	.console-header {
		display: flex;
		align-items: center;
		gap: 1.5rem;
		padding: 0 1.25rem;
		height: 56px;
		background: var(--celine-bg-elevated);
		border-bottom: 1px solid var(--celine-border);
		position: sticky;
		top: 0;
		z-index: 20;
	}

	.brand {
		font-weight: 700;
		color: var(--celine-primary);
		text-decoration: none;
		white-space: nowrap;
	}

	.nav {
		display: flex;
		gap: 0.25rem;
	}

	.nav a {
		padding: 0.375rem 0.75rem;
		border-radius: var(--celine-radius-sm);
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--celine-text-secondary);
		text-decoration: none;
	}

	.nav a:hover {
		background: var(--celine-bg);
	}

	.nav a.active {
		background: var(--celine-primary-light, #ccfbf1);
		color: var(--celine-primary);
	}

	.header-right {
		margin-left: auto;
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}

	.rec-switcher {
		min-height: 2.25rem;
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		background: var(--celine-bg-elevated);
		color: var(--celine-text);
		padding: 0 0.5rem;
		font-size: 0.875rem;
	}

	.avatar {
		width: 34px;
		height: 34px;
		border-radius: 50%;
		border: none;
		background: var(--celine-primary);
		color: #fff;
		font-size: 0.75rem;
		font-weight: 700;
		cursor: pointer;
	}

	.dropdown {
		position: absolute;
		top: calc(100% + 8px);
		right: 0;
		min-width: 260px;
		background: var(--celine-bg-elevated);
		border: 1px solid var(--celine-border);
		border-radius: var(--celine-radius-sm);
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
		padding: 0.5rem 0;
		z-index: 30;
	}

	.dropdown-id {
		display: flex;
		flex-direction: column;
		padding: 0.5rem 1rem;
		font-size: 0.8125rem;
	}

	.dropdown-id span {
		color: var(--celine-text-secondary);
	}

	.dropdown-caps {
		padding: 0.25rem 1rem 0.5rem;
		font-size: 0.75rem;
		color: var(--celine-text-secondary);
	}

	.dropdown-caps ul {
		margin: 0.25rem 0 0;
		padding-left: 1rem;
	}

	.dropdown hr {
		border: none;
		border-top: 1px solid var(--celine-border);
		margin: 0.25rem 0;
	}

	.dropdown a {
		display: block;
		padding: 0.5rem 1rem;
		font-size: 0.875rem;
		color: var(--celine-text);
		text-decoration: none;
	}

	.dropdown a:hover {
		background: var(--celine-bg);
	}

	.console-main {
		flex: 1;
		padding: 1.5rem 1.25rem 3rem;
		max-width: 1400px;
		width: 100%;
		margin: 0 auto;
	}
</style>
