<script>
  import { onMount } from "svelte";
  import { api } from "../api.js";
  import { workspaceJob } from "./jobs.svelte.js";

  let { onSignOut, onStatus = null } = $props();

  let status = $state(null);
  let members = $state([]);
  let membersErr = $state(null);
  let invite = $state(null); // { url, expires_at }
  let inviteErr = $state(null);
  let inviteBusy = $state(false);
  let copied = $state(false);
  let removeErr = $state(null);

  async function loadStatus({ refresh = false } = {}) {
    try {
      status = await api.workspaceStatus(refresh);
      onStatus?.(status);
    } catch (e) {
      membersErr = e.message;
    }
  }

  async function loadMembers() {
    membersErr = null;
    try {
      members = (await api.workspaceMembers()).members;
    } catch (e) {
      membersErr = e.message;
    }
  }

  let binding = $state(null); // { status, bound_workspace }
  let bindingDismissed = $state(false); // session-local "Not now"
  let bindingBusy = $state(false);
  let bindingErr = $state(null);

  async function loadBinding() {
    try {
      binding = await api.workspaceBinding();
    } catch {
      binding = null; // signed out or store unavailable — no banner
    }
  }

  async function uploadToWorkspace() {
    bindingBusy = true;
    bindingErr = null;
    try {
      await api.workspaceBindingUpload();
      await loadBinding();
      syncNow();
    } catch (e) {
      bindingErr = e.message;
    } finally {
      bindingBusy = false;
    }
  }

  onMount(async () => {
    // Refresh session state from the service when possible (falls back to
    // the local cache offline), then the member list.
    await loadStatus({ refresh: true });
    if (status?.signed_in) {
      loadMembers();
      loadBinding();
    }
  });

  // A finished sync updates last-synced state — re-pull the cache.
  $effect(() => {
    workspaceJob.finishedCount;
    loadStatus();
  });

  async function syncNow() {
    if (workspaceJob.running) return;
    await workspaceJob.start("Workspace sync", "workspace-sync", () => api.workspaceSync());
  }

  async function makeInvite() {
    inviteBusy = true;
    inviteErr = null;
    copied = false;
    try {
      invite = await api.workspaceInvite("member");
    } catch (e) {
      inviteErr = e.message;
    } finally {
      inviteBusy = false;
    }
  }

  async function copyInvite() {
    try {
      await navigator.clipboard.writeText(invite.url);
      copied = true;
    } catch {
      // clipboard blocked — the link is selectable below
    }
  }

  let resetLink = $state(null); // { url, name, expires_at }
  let resetErr = $state(null);
  let resetCopied = $state(false);

  async function makeResetLink(m) {
    resetErr = null;
    resetCopied = false;
    try {
      resetLink = await api.workspaceMemberResetLink(m.user_id);
    } catch (e) {
      resetErr = e.message;
    }
  }

  async function copyResetLink() {
    try {
      await navigator.clipboard.writeText(resetLink.url);
      resetCopied = true;
    } catch {
      // clipboard blocked — the link is selectable below
    }
  }

  async function removeMember(m) {
    if (!confirm(`Remove ${m.name} (${m.email}) from the workspace?`)) return;
    removeErr = null;
    try {
      await api.workspaceRemoveMember(m.user_id);
      loadMembers();
    } catch (e) {
      removeErr = e.message;
    }
  }

  async function signOut() {
    await api.workspaceSignOut();
    onSignOut?.();
  }

  const isAdmin = $derived(status?.role === "admin");
  const lastSynced = $derived(
    status?.last_synced_at ? new Date(status.last_synced_at).toLocaleString() : null
  );

  // Danger zone (M22): deletion is deliberate — expanded panel + password.
  let dangerOpen = $state(null); // null | 'workspace' | 'account'
  let dangerPassword = $state("");
  let dangerBusy = $state(false);
  let dangerErr = $state(null);

  function openDanger(kind) {
    dangerOpen = dangerOpen === kind ? null : kind;
    dangerPassword = "";
    dangerErr = null;
  }

  async function confirmDanger() {
    dangerBusy = true;
    dangerErr = null;
    try {
      if (dangerOpen === "workspace") await api.workspaceDelete(dangerPassword);
      else await api.accountDelete(dangerPassword);
      onSignOut?.();
    } catch (e) {
      dangerErr = e.message;
    } finally {
      dangerBusy = false;
    }
  }
</script>

<div class="stack">
  {#if binding?.status === "needs_decision" && !bindingDismissed}
    <div class="card notice-banner">
      <h3>Your local knowledge isn't in this workspace yet</h3>
      <p class="muted">
        Everything in the Knowledge tab lives on this computer — it's always
        visible to you here, but it has <strong>not</strong> been uploaded:
        this machine last synced with a different workspace (or one that no
        longer exists). Upload it to make it part of
        <strong>{status?.workspace || "this workspace"}</strong> (shared with
        its members and your other devices), or hold off and nothing leaves
        this machine.
      </p>
      <div class="row">
        <button class="primary" disabled={bindingBusy} onclick={uploadToWorkspace}>
          {#if bindingBusy}<span class="spinner"></span> Preparing…{:else}Upload it to this workspace{/if}
        </button>
        <button onclick={() => (bindingDismissed = true)}>Not now</button>
      </div>
      {#if bindingErr}<p class="error">{bindingErr}</p>{/if}
    </div>
  {/if}

  <div class="card stack">
    <div class="row spread">
      <h2 class="display">{status?.workspace || "Workspace"}</h2>
      <button onclick={signOut}>Sign out</button>
    </div>
    <p class="muted">Signed in as {status?.email} ({status?.role})</p>

    <div class="row" style="gap: 12px; align-items: center">
      <button class="primary" disabled={workspaceJob.running} onclick={syncNow}>
        {#if workspaceJob.running}<span class="spinner"></span> Syncing…{:else}Sync now{/if}
      </button>
      {#if lastSynced}<span class="muted">last synced {lastSynced}</span>{/if}
    </div>
    {#if workspaceJob.err}
      <p class="error">{workspaceJob.err}</p>
    {:else if workspaceJob.result?.kind === "workspace-sync"}
      <p class="muted">
        ✓ Synced — sent {workspaceJob.result.pushed}, received {workspaceJob.result.applied}
        {#if workspaceJob.result.quarantined}
          · <span class="error">{workspaceJob.result.quarantined} change(s) quarantined</span>
        {/if}
      </p>
    {/if}
    {#if status?.quarantine_count}
      <p class="error">
        ⚠ {status.quarantine_count} synced change(s) could not be applied on this
        device (evidence check or missing dependency). They retry on every sync.
      </p>
    {/if}
  </div>

  <div class="card stack">
    <h3>Team</h3>
    {#if membersErr}<p class="error">{membersErr}</p>{/if}
    <ul class="member-list">
      {#each members.filter((m) => m.active) as m (m.user_id)}
        <li class="row spread">
          <span>
            {m.name} <span class="muted">· {m.email} · {m.role}</span>
          </span>
          {#if isAdmin && m.user_id !== status?.user_id}
            <span class="row" style="gap: 8px">
              <button onclick={() => makeResetLink(m)}>Reset password</button>
              <button onclick={() => removeMember(m)}>Remove</button>
            </span>
          {/if}
        </li>
      {/each}
    </ul>
    {#if removeErr}<p class="error">{removeErr}</p>{/if}
    {#if resetErr}<p class="error">{resetErr}</p>{/if}
    {#if resetLink}
      <div class="stack">
        <div class="row" style="gap: 8px; align-items: center">
          <input readonly value={resetLink.url} style="flex: 1" onfocus={(e) => e.target.select()} />
          <button class="primary" onclick={copyResetLink}>{resetCopied ? "Copied ✓" : "Copy"}</button>
        </div>
        <p class="muted">
          Send this to {resetLink.name} over any channel — it lets them choose a
          new password. Single use, expires in 60 minutes. Setting a new
          password signs them out everywhere.
        </p>
      </div>
    {/if}

    {#if isAdmin}
      <div class="stack" style="margin-top: 8px">
        <div class="row" style="gap: 8px; align-items: center">
          <button disabled={inviteBusy} onclick={makeInvite}>
            {#if inviteBusy}<span class="spinner"></span>{:else}Generate invite link{/if}
          </button>
        </div>
        {#if invite}
          <div class="row" style="gap: 8px; align-items: center">
            <input readonly value={invite.url} style="flex: 1" onfocus={(e) => e.target.select()} />
            <button class="primary" onclick={copyInvite}>{copied ? "Copied ✓" : "Copy"}</button>
          </div>
          <p class="muted">
            Send this link to your teammate — it lets them create their account
            and join the workspace. One use, expires {new Date(invite.expires_at).toLocaleDateString()}.
          </p>
        {/if}
        {#if inviteErr}<p class="error">{inviteErr}</p>{/if}
      </div>
    {/if}
  </div>

  <div class="card stack danger-zone">
    <h3>Danger zone</h3>
    <div class="row" style="gap: 8px">
      {#if isAdmin}
        <button class="danger-btn" onclick={() => openDanger("workspace")}>Delete workspace…</button>
      {/if}
      <button class="danger-btn" onclick={() => openDanger("account")}>Delete my account…</button>
    </div>
    {#if dangerOpen === "workspace"}
      <div class="stack">
        <p class="muted">
          This permanently deletes the <strong>{status?.workspace}</strong>
          workspace for everyone: all synced knowledge and member accounts
          are erased from the workspace server, and every member is signed
          out. The knowledge stored on this computer is <strong>not</strong>
          deleted — it stays here and keeps working.
        </p>
      </div>
    {:else if dangerOpen === "account"}
      <div class="stack">
        <p class="muted">
          This permanently deletes your account. If you're the only person in
          the workspace, the workspace is deleted with it. If teammates
          remain, you leave the workspace and your login is
          erased. Knowledge stored on this computer is <strong>not</strong>
          deleted.
        </p>
      </div>
    {/if}
    {#if dangerOpen}
      <div class="row" style="gap: 8px; align-items: center">
        <input
          type="password"
          bind:value={dangerPassword}
          placeholder="Confirm your password"
          style="flex: 1"
        />
        <button
          class="danger-btn confirm"
          disabled={dangerBusy || !dangerPassword}
          onclick={confirmDanger}
        >
          {#if dangerBusy}<span class="spinner"></span> Deleting…{:else}
            {dangerOpen === "workspace" ? "Delete workspace permanently" : "Delete my account permanently"}{/if}
        </button>
      </div>
      {#if dangerErr}<p class="error">{dangerErr}</p>{/if}
    {/if}
  </div>
</div>

<style>
  .member-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .notice-banner {
    border-color: var(--warn);
  }
  .danger-zone {
    border-color: var(--bad);
  }
  .danger-btn {
    color: var(--bad);
    border-color: var(--bad);
  }
  .danger-btn.confirm {
    background: var(--bad);
    color: #f7f1e3;
  }
</style>
