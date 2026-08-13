<script>
  // The Graphs tab: every graph this machine belongs to, joining a friend's
  // graph by invite link, and Host mode — this machine serving graphs.
  import { onMount } from "svelte";
  import { api } from "../api.js";
  import { graphsStore } from "./graphs.svelte.js";
  import { workspaceJob } from "./jobs.svelte.js";

  let err = $state(null);

  // -- join a graph ----------------------------------------------------------
  let joinUrl = $state("");
  let joinHandle = $state("");
  let joinName = $state("");
  let joinBusy = $state(false);
  let joinErr = $state(null);

  async function joinGraph() {
    if (!joinUrl.trim() || !joinHandle.trim() || joinBusy) return;
    joinBusy = true;
    joinErr = null;
    try {
      const joined = await api.graphsJoin({
        invite_url: joinUrl.trim(),
        handle: joinHandle.trim(),
        display_name: joinName.trim(),
      });
      joinUrl = "";
      await graphsStore.refresh();
      graphsStore.setActive(joined.id);
      if (joined.sync_job_id) {
        workspaceJob.attach(joined.sync_job_id, "First sync", "workspace-sync");
      }
    } catch (e) {
      joinErr = e.message;
    } finally {
      joinBusy = false;
    }
  }

  // -- per-graph actions -------------------------------------------------------
  let syncBusy = $state({}); // graph id → bool
  async function syncNow(g) {
    syncBusy = { ...syncBusy, [g.id]: true };
    try {
      const { job_id } = await api.graphSync(g.id);
      workspaceJob.attach(job_id, `Sync ${g.name}`, "workspace-sync");
    } catch (e) {
      err = e.message;
    } finally {
      syncBusy = { ...syncBusy, [g.id]: false };
    }
  }

  async function leaveGraph(g) {
    if (!confirm(`Remove "${g.name}" from this computer? The local copy is deleted; the host's copy (and other members') survives.`)) return;
    try {
      await graphsStore.remove(g.id, true);
    } catch (e) {
      err = e.message;
    }
  }

  // A finished sync updates last_synced_at — refresh the list, with
  // quarantine counts (divergence must be visible, never silent).
  let quarantine = $state({}); // graph id → count
  async function refreshCounts() {
    try {
      const rows = await api.graphsList(true);
      quarantine = Object.fromEntries(
        rows.map((g) => [g.id, g.quarantine_count ?? 0])
      );
    } catch {}
  }
  $effect(() => {
    void workspaceJob.finishedCount;
    graphsStore.refresh().catch(() => {});
    refreshCounts();
  });

  // -- members panel (host/admin of the selected graph) -------------------------
  let membersFor = $state(null); // graph id with the panel open
  let members = $state([]);
  let membersErr = $state(null);
  let inviteLink = $state(null);
  let inviteCopied = $state(false);
  let inviteRole = $state("member");

  async function openMembers(g) {
    membersFor = membersFor === g.id ? null : g.id;
    inviteLink = null;
    inviteCopied = false;
    if (membersFor) await loadMembers(g.id);
  }

  async function loadMembers(id) {
    membersErr = null;
    try {
      members = (await api.graphMembers(id)).members;
    } catch (e) {
      membersErr = e.message;
      members = [];
    }
  }

  async function mintInvite(id) {
    membersErr = null;
    try {
      const resp = await api.graphInvite(id, inviteRole);
      inviteLink = resp.url;
      inviteCopied = false;
    } catch (e) {
      membersErr = e.message;
    }
  }

  async function copyInvite() {
    try {
      await navigator.clipboard.writeText(inviteLink);
      inviteCopied = true;
    } catch {}
  }

  async function removeMember(id, m) {
    if (!confirm(`Remove @${m.handle} from this graph?`)) return;
    try {
      await api.graphRemoveMember(id, m.principal_id);
      await loadMembers(id);
    } catch (e) {
      membersErr = e.message;
    }
  }

  async function setRole(id, m, role) {
    try {
      await api.graphSetRole(id, m.principal_id, role);
      await loadMembers(id);
    } catch (e) {
      membersErr = e.message;
      await loadMembers(id);
    }
  }

  // -- host mode -----------------------------------------------------------------
  let host = $state(null); // GET /api/host snapshot
  let hostErr = $state(null);
  let advertiseChoice = $state("");
  let advertiseManual = $state("");
  let createName = $state("");
  let createHandle = $state("");
  let createBusy = $state(false);
  let createErr = $state(null);

  async function refreshHost() {
    try {
      host = await api.hostStatus();
      if (host.advertise_url_set) advertiseChoice = host.advertise_url;
    } catch (e) {
      hostErr = e.message;
    }
  }

  async function toggleHosting() {
    hostErr = null;
    try {
      host = await api.hostConfigure({ enabled: !host.enabled });
    } catch (e) {
      hostErr = e.message;
    }
  }

  async function saveAdvertise() {
    hostErr = null;
    const url = advertiseChoice === "__manual" ? advertiseManual.trim() : advertiseChoice;
    try {
      host = await api.hostConfigure({ advertise_url: url });
    } catch (e) {
      hostErr = e.message;
    }
  }

  async function createHostedGraph() {
    if (!createName.trim() || createBusy) return;
    createBusy = true;
    createErr = null;
    try {
      const created = await api.hostCreateGraph({
        name: createName.trim(),
        handle: createHandle.trim() || "host",
        display_name: "",
      });
      createName = "";
      await graphsStore.refresh();
      graphsStore.setActive(created.id);
    } catch (e) {
      createErr = e.message;
    } finally {
      createBusy = false;
    }
  }

  // Remote server (VPS) — same create, different address; the server must
  // already know this identity as its owner (`python -m cloud set-owner`).
  let remoteUrl = $state("");
  let remoteName = $state("");
  let remoteHandle = $state("");
  let remoteBusy = $state(false);
  let remoteErr = $state(null);

  async function createRemoteGraph() {
    if (!remoteUrl.trim() || !remoteName.trim() || remoteBusy) return;
    remoteBusy = true;
    remoteErr = null;
    try {
      const created = await api.hostCreateGraph({
        name: remoteName.trim(),
        handle: remoteHandle.trim() || "host",
        display_name: "",
        host_url: remoteUrl.trim(),
      });
      remoteName = "";
      await graphsStore.refresh();
      graphsStore.setActive(created.id);
    } catch (e) {
      remoteErr = e.message;
    } finally {
      remoteBusy = false;
    }
  }

  onMount(() => {
    graphsStore.refresh().catch(() => {});
    refreshHost();
  });

  const shared = $derived(graphsStore.list.filter((g) => g.kind !== "private"));
  const canManage = (g) => g.role === "host" || g.role === "admin";
</script>

<div class="card stack">
  <h2 class="display">Your graphs</h2>
  <p class="muted">
    Your private graph lives only on this computer. Shared graphs sync with a
    host — a friend's always-on machine, or this one (Host mode below).
  </p>

  {#if err}<p class="error">{err}</p>{/if}

  <div class="graph-row">
    <div class="graph-info">
      <strong>My graph</strong>
      <span class="muted" style="font-size:12px">private — never leaves this computer</span>
    </div>
  </div>

  {#each shared as g (g.id)}
    <div class="graph-row">
      <div class="graph-info">
        <strong>{g.name}</strong>
        <span class="muted" style="font-size:12px">
          {#if g.host_url && g.workspace_id}
            {g.role ?? "member"}{g.handle ? ` · @${g.handle}` : ""} · {g.host_url}
            {#if g.last_synced_at}
              · synced {new Date(g.last_synced_at).toLocaleTimeString()}
            {:else}
              · never synced
            {/if}
            {#if quarantine[g.id]}
              · <span class="error" style="font-size:12px">{quarantine[g.id]} change{quarantine[g.id] === 1 ? "" : "s"} quarantined</span>
            {/if}
          {:else}
            local only — not connected to a host
          {/if}
        </span>
      </div>
      <div class="row" style="gap:8px">
        {#if g.host_url && g.workspace_id}
          <button disabled={syncBusy[g.id]} onclick={() => syncNow(g)}>Sync now</button>
          {#if canManage(g)}
            <button onclick={() => openMembers(g)}>
              {membersFor === g.id ? "Hide members" : "Members"}
            </button>
          {/if}
        {/if}
        <button class="danger" onclick={() => leaveGraph(g)}>Leave</button>
      </div>
    </div>
    {#if membersFor === g.id}
      <div class="members-panel stack">
        {#if membersErr}<p class="error">{membersErr}</p>{/if}
        {#each members as m (m.principal_id)}
          <div class="row spread">
            <span>
              <strong>@{m.handle}</strong>
              <span class="muted" style="font-size:12px"> {m.display_name} · {m.role}{m.active ? "" : " · removed"}</span>
            </span>
            {#if m.role !== "host" && m.active}
              <span class="row" style="gap:6px">
                {#if g.role === "host"}
                  <select
                    value={m.role}
                    onchange={(e) => setRole(g.id, m, e.target.value)}
                  >
                    <option value="member">member</option>
                    <option value="admin">admin</option>
                  </select>
                {/if}
                {#if g.role === "host" || m.role === "member"}
                  <button class="danger" onclick={() => removeMember(g.id, m)}>Remove</button>
                {/if}
              </span>
            {/if}
          </div>
        {/each}
        <div class="row" style="gap:8px">
          {#if g.role === "host"}
            <select bind:value={inviteRole}>
              <option value="member">member invite</option>
              <option value="admin">admin invite</option>
            </select>
          {/if}
          <button onclick={() => mintInvite(g.id)}>New invite link</button>
        </div>
        {#if inviteLink}
          <div class="row" style="gap:8px">
            <code class="invite-link">{inviteLink}</code>
            <button onclick={copyInvite}>{inviteCopied ? "Copied" : "Copy"}</button>
          </div>
          <p class="muted" style="font-size:12px">
            Single use, expires in 14 days. Send it over any channel.
          </p>
        {/if}
      </div>
    {/if}
  {/each}
</div>

<div class="card stack" style="margin-top:16px">
  <h3 class="display" style="margin:0">Join a graph</h3>
  <p class="muted" style="margin:0">
    Paste an invite link from a friend and pick the name you'll go by there.
  </p>
  <div>
    <label class="field-label" for="join-url">Invite link</label>
    <input id="join-url" type="text" bind:value={joinUrl} placeholder="http://…/invite-accept?token=…" />
  </div>
  <div class="row" style="gap:10px">
    <div style="flex:1">
      <label class="field-label" for="join-handle">Handle</label>
      <input id="join-handle" type="text" bind:value={joinHandle} placeholder="mo" />
    </div>
    <div style="flex:2">
      <label class="field-label" for="join-name">Display name (optional)</label>
      <input id="join-name" type="text" bind:value={joinName} placeholder="Mo" />
    </div>
  </div>
  <div class="row">
    <button class="primary" disabled={joinBusy || !joinUrl.trim() || !joinHandle.trim()} onclick={joinGraph}>
      {joinBusy ? "Joining…" : "Join"}
    </button>
  </div>
  {#if joinErr}<p class="error">{joinErr}</p>{/if}
</div>

<div class="card stack" style="margin-top:16px">
  <div class="row spread">
    <h3 class="display" style="margin:0">Host graphs on this machine</h3>
    {#if host}
      <button class={host.enabled ? "danger" : "primary"} onclick={toggleHosting}>
        {host.enabled ? "Turn hosting off" : "Turn hosting on"}
      </button>
    {/if}
  </div>
  <p class="muted" style="margin:0">
    Like hosting a game server: your always-on machine holds the graph and
    your friends' apps sync with it. Reach it however you like — a Tailscale
    address (no router setup), port forwarding, or a public host name.
  </p>
  {#if hostErr}<p class="error">{hostErr}</p>{/if}
  {#if host?.enabled}
    <p class="muted" style="font-size:13px; margin:0">
      {host.running ? `Serving on port ${host.port}.` : "Starting…"}
      Invite links use <code>{host.advertise_url}</code>
    </p>
    <div>
      <label class="field-label" for="advertise">Address members connect to</label>
      <div class="row" style="gap:8px">
        <select id="advertise" bind:value={advertiseChoice}>
          {#each host.candidates as c (c.url)}
            <option value={c.url}>{c.url} ({c.kind})</option>
          {/each}
          {#if host.advertise_url_set && !host.candidates.some((c) => c.url === host.advertise_url)}
            <option value={host.advertise_url}>{host.advertise_url} (current)</option>
          {/if}
          <option value="__manual">Enter manually…</option>
        </select>
        {#if advertiseChoice === "__manual"}
          <input type="text" bind:value={advertiseManual} placeholder="http://my-host.example:8788" style="flex:1" />
        {/if}
        <button onclick={saveAdvertise}>Use this address</button>
      </div>
    </div>
    <div style="margin-top:8px">
      <label class="field-label" for="new-hosted">Create a shared graph here</label>
      <div class="row" style="gap:8px">
        <input id="new-hosted" type="text" bind:value={createName} placeholder="Graph name" style="flex:2" />
        <input type="text" bind:value={createHandle} placeholder="your handle (host)" style="flex:1" />
        <button class="primary" disabled={createBusy || !createName.trim()} onclick={createHostedGraph}>
          {createBusy ? "Creating…" : "Create"}
        </button>
      </div>
      {#if createErr}<p class="error">{createErr}</p>{/if}
    </div>
  {/if}

  <details style="margin-top:8px">
    <summary class="muted" style="cursor:pointer; font-size:13px">
      Or create a graph on a remote server (a VPS you set up)
    </summary>
    <div class="stack" style="margin-top:10px">
      <p class="muted" style="font-size:12px; margin:0">
        The server must know your key as its owner — run
        <code>python -m cloud set-owner &lt;your public key&gt;</code> there first
        (your key is in Settings → Identity).
      </p>
      <div class="row" style="gap:8px">
        <input type="text" bind:value={remoteUrl} placeholder="http://my-vps.example:8787" style="flex:2" />
        <input type="text" bind:value={remoteName} placeholder="Graph name" style="flex:2" />
        <input type="text" bind:value={remoteHandle} placeholder="handle" style="flex:1" />
        <button disabled={remoteBusy || !remoteUrl.trim() || !remoteName.trim()} onclick={createRemoteGraph}>
          {remoteBusy ? "Creating…" : "Create"}
        </button>
      </div>
      {#if remoteErr}<p class="error">{remoteErr}</p>{/if}
    </div>
  </details>
</div>

<style>
  .graph-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 0;
    border-top: 1px solid var(--border);
  }
  .graph-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .members-panel {
    margin: 0 0 10px;
    padding: 12px;
    border: 1px solid var(--border);
    background: var(--panel-2);
  }
  .invite-link {
    font-size: 11px;
    word-break: break-all;
    padding: 6px 8px;
    border: 1px solid var(--border);
    background: var(--panel);
    user-select: all;
  }
</style>
