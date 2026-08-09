<script>
  // Getting-started checklist: a corner card tracking real first-run progress.
  // Completion is re-derived from actual state every launch (via GET
  // /api/onboarding), so returning users never see stale unchecked items.
  // Self-hides when everything applicable is done; the × dismiss persists.
  import { onMount } from "svelte";
  import { api } from "../api.js";

  let { status, isAdmin, onNavigate, onReplayTour, onDismiss } = $props();

  // Admin-only, cloud-backed check — fetched lazily here (not in the local
  // /api/onboarding route) and fail-soft: offline just leaves it unchecked.
  let teamInvited = $state(false);

  onMount(async () => {
    if (!isAdmin) return;
    try {
      const { members } = await api.workspaceMembers();
      teamInvited = (members?.length ?? 0) > 1;
    } catch {
      // unreachable cloud — leave unchecked
    }
  });

  const items = $derived([
    { label: "Take the tour", done: status.tour_done, go: () => onReplayTour?.() },
    {
      label: "Connect & sync a source",
      done: status.source_synced,
      go: () => onNavigate?.("sources"),
    },
    {
      label: "Capture a meeting",
      done: status.meeting_captured,
      go: () => onNavigate?.("capture"),
    },
    ...(isAdmin
      ? [{ label: "Invite a teammate", done: teamInvited, go: () => onNavigate?.("workspace") }]
      : []),
  ]);
  const doneCount = $derived(items.filter((i) => i.done).length);
  const allDone = $derived(doneCount === items.length);
</script>

{#if !allDone}
  <div class="getting-started card stack">
    <div class="row spread">
      <strong>Getting started</strong>
      <span class="row" style="gap:8px">
        <span class="muted">{doneCount}/{items.length}</span>
        <button class="dismiss" aria-label="Dismiss" onclick={() => onDismiss?.()}>×</button>
      </span>
    </div>
    <ul class="checklist">
      {#each items as item}
        <li>
          <button class="item" class:done={item.done} onclick={item.go}>
            <span class="check">{item.done ? "✓" : "○"}</span>
            {item.label}
          </button>
        </li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  .getting-started {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 40; /* above content, below the tour backdrop (60) */
    width: 280px;
  }
  .checklist {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .item {
    background: none;
    border: none;
    color: var(--text);
    cursor: pointer;
    padding: 6px 4px;
    width: 100%;
    text-align: left;
    display: flex;
    gap: 8px;
    align-items: baseline;
    border-radius: 6px;
  }
  .item:hover {
    background: color-mix(in srgb, var(--accent) 12%, transparent);
  }
  .item.done {
    color: var(--muted);
  }
  .check {
    color: var(--accent);
  }
  .dismiss {
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    font-size: 1.1em;
    line-height: 1;
    padding: 2px 4px;
  }
  .dismiss:hover {
    color: var(--text);
  }
</style>
