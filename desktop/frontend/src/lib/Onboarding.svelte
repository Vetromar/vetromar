<script>
  // First-run welcome tour: a handful of step cards over a backdrop. Purely
  // presentational — App owns when it opens and persists tour_done on close.
  import { onMount } from "svelte";
  import { api } from "../api.js";

  let { role, workspaceName, onClose, onNavigate } = $props();

  // The agent-paste prompt (server-composed, references the MCP shim the
  // ui_server installs at boot). Fail-soft: without it the card falls back to
  // a plain sentence instead of a paste box.
  let mcpPrompt = $state(null);
  let copied = $state(false);

  onMount(async () => {
    try {
      mcpPrompt = (await api.mcpInfo()).prompt;
    } catch {}
  });

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(mcpPrompt);
      copied = true;
    } catch {
      // Webview without clipboard API permission: select-and-copy fallback.
      const ta = document.createElement("textarea");
      ta.value = mcpPrompt;
      document.body.appendChild(ta);
      ta.select();
      try {
        copied = document.execCommand("copy");
      } finally {
        ta.remove();
      }
    }
  }

  const isAdmin = role === "admin";
  const welcomeNote = workspaceName
    ? isAdmin
      ? `You're the admin of ${workspaceName} — you can invite your team when you're ready.`
      : `You're part of ${workspaceName} — everything here syncs across your team automatically.`
    : null;

  const cards = [
    {
      title: "Welcome to Vetromar",
      body: "Vetromar turns your team's meetings, docs, and tools into one living knowledge base — every decision, linked and searchable.",
      note: welcomeNote,
    },
    {
      title: "Capture a meeting",
      body: "Record live or import an audio file. Vetromar transcribes it, identifies speakers, and extracts the decisions — with quotes to back them up.",
      go: { label: "Go to Capture", tab: "capture" },
    },
    {
      title: "Connect your tools",
      body: "One click connects Notion, Linear, and more. Sync pulls what your team already wrote into the same knowledge base.",
      go: { label: "Go to Sources", tab: "sources" },
    },
    {
      title: "Your knowledge graph",
      body: "Everything you capture or sync lands in one linked graph — decisions, people, and projects, connected. Explore it in 3D on the Knowledge tab.",
      go: { label: "Go to Knowledge", tab: "knowledge" },
    },
    {
      title: "Your AI agents already know the way in",
      body: "Your AI agent can query your Vetromar knowledge base. Just paste this prompt into your agent:",
      fallbackBody:
        "Vetromar speaks MCP, the language AI agents use to reach tools. Just ask your agent to connect to Vetromar — and it can answer from everything your team knows.",
      mcp: true,
    },
    ...(isAdmin
      ? [
          {
            title: "Invite your team",
            body: "Vetromar gets better with every teammate — everyone's meetings and sources land in the same shared graph. Send an invite from the Workspace tab.",
            go: { label: "Go to Workspace", tab: "workspace" },
          },
        ]
      : []),
  ];

  let step = $state(0);
  const card = $derived(cards[step]);
  const last = $derived(step === cards.length - 1);

  function onKeydown(e) {
    if (e.key === "Escape") onClose?.();
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="backdrop">
  <div class="tour-card stack" role="dialog" aria-label="Welcome tour">
    <div class="row spread">
      <h2 class="display">{card.title}</h2>
      <span class="muted step-count">{step + 1}/{cards.length}</span>
    </div>
    {#if card.mcp && !mcpPrompt}
      <p>{card.fallbackBody}</p>
    {:else}
      <p>{card.body}</p>
    {/if}
    {#if card.note}
      <p class="muted">{card.note}</p>
    {/if}
    {#if card.mcp && mcpPrompt}
      <div class="prompt-box">{mcpPrompt}</div>
      <div class="row">
        <button onclick={copyPrompt}>Copy prompt</button>
        {#if copied}<span class="muted">✓ Copied</span>{/if}
      </div>
    {/if}
    <div class="dots" aria-hidden="true">
      {#each cards as _, i}
        <span class="dot" class:active={i === step}></span>
      {/each}
    </div>
    <div class="row spread footer">
      <button class="skip" onclick={() => onClose?.()}>Skip tour</button>
      <div class="row">
        {#if step > 0}
          <button onclick={() => (step -= 1)}>Back</button>
        {/if}
        {#if card.go}
          <button onclick={() => onNavigate?.(card.go.tab)}>{card.go.label}</button>
        {/if}
        {#if last}
          <button class="primary" onclick={() => onClose?.()}>Done</button>
        {:else}
          <button class="primary" onclick={() => (step += 1)}>Next</button>
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: 60; /* above the app header (z-index 5) and the checklist (40) */
    background: rgba(0, 0, 0, 0.5);
    display: grid;
    place-items: center;
  }
  .tour-card {
    width: min(440px, 90vw);
    background: var(--panel);
    border: 2px solid var(--border);
    padding: 24px;
  }
  .step-count {
    font-size: 0.85em;
  }
  .dots {
    display: flex;
    gap: 6px;
    justify-content: center;
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: color-mix(in srgb, var(--accent) 25%, transparent);
  }
  .dot.active {
    background: var(--accent);
  }
  .footer {
    margin-top: 4px;
  }
  .prompt-box {
    font-family: var(--font-mono);
    font-size: 0.8em;
    line-height: 1.5;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    border: 2px solid var(--border);
    padding: 10px 12px;
    user-select: text;
    max-height: 140px;
    overflow-y: auto;
  }
  .skip {
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    padding: 8px 0;
  }
  .skip:hover {
    color: var(--text);
  }
</style>
