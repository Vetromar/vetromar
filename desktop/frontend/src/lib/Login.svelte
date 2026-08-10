<script>
  import { api } from "../api.js";

  // embedded: rendered inside the Workspace tab's connect card (no page
  // centering, no outer card chrome, shorter copy).
  let { serverUrl = "", onSignedIn, embedded = false } = $props();

  let email = $state("");
  let password = $state("");
  let busy = $state(false);
  let err = $state(null);

  // Self-hosted: the workspace server this app talks to, editable here.
  let showServer = $state(false);
  let serverInput = $state(serverUrl || "");
  let serverBusy = $state(false);
  let serverErr = $state(null);
  let currentServer = $state(serverUrl || "");
  $effect(() => {
    // Adopt the prop once it arrives (App fetches status async).
    if (serverUrl && !currentServer) {
      currentServer = serverUrl;
      if (!serverInput) serverInput = serverUrl;
    }
  });

  async function saveServer() {
    serverBusy = true;
    serverErr = null;
    try {
      const r = await api.workspaceServerUrl(serverInput.trim());
      currentServer = r.url;
      serverInput = r.url;
      showServer = false;
    } catch (ex) {
      serverErr = ex.message;
    } finally {
      serverBusy = false;
    }
  }

  async function submit(e) {
    e?.preventDefault();
    busy = true;
    err = null;
    try {
      const status = await api.workspaceSignIn(email.trim(), password);
      onSignedIn?.(status);
    } catch (ex) {
      err = ex.message;
    } finally {
      busy = false;
    }
  }

  // target="_blank" goes nowhere in the Tauri webview (no opener plugin) —
  // the sidecar opens the system browser.
  let createUrlFallback = $state(null);

  async function openCreatePage() {
    createUrlFallback = null;
    try {
      const r = await api.workspaceOpenSignup();
      if (!r.opened) createUrlFallback = r.url;
    } catch {
      createUrlFallback = (currentServer || "").replace(/\/$/, "") + "/signup";
    }
  }

  let showReset = $state(false);
</script>

<div class="stack login-card" class:card={!embedded} class:embedded>
  {#if !embedded}
    <h2 class="display">Sign in to your workspace</h2>
    <p class="muted">
      Vetromar syncs your team's knowledge across everyone's devices through a
      workspace server your team runs. Sign in with your account, or the invite
      your admin sent you.
    </p>
  {/if}
  <p class="muted server-line">
    Server: <span class="server-url">{currentServer || "not set"}</span>
    <button class="linklike" type="button" onclick={() => (showServer = !showServer)}>
      change
    </button>
  </p>
  {#if showServer}
    <div class="stack">
      <input
        placeholder="https://vetromar.your-company.com or http://localhost:8787"
        bind:value={serverInput}
      />
      <div class="row">
        <button class="primary" type="button" disabled={serverBusy || !serverInput.trim()} onclick={saveServer}>
          {#if serverBusy}<span class="spinner"></span>{:else}Save{/if}
        </button>
        <button type="button" onclick={() => (showServer = false)}>Cancel</button>
      </div>
      {#if serverErr}<p class="error">{serverErr}</p>{/if}
    </div>
  {/if}
  <form class="stack" onsubmit={submit}>
    <div>
      <label class="field-label" for="login-email">Email</label>
      <input id="login-email" type="email" bind:value={email} placeholder="you@company.com" />
    </div>
    <div>
      <label class="field-label" for="login-password">Password</label>
      <input id="login-password" type="password" bind:value={password} placeholder="••••••••" />
    </div>
    <div class="row">
      <button class="primary" type="submit" disabled={busy || !email.trim() || !password}>
        {#if busy}<span class="spinner"></span> Signing in…{:else}Sign in{/if}
      </button>
    </div>
  </form>
  {#if err}<p class="error">{err}</p>{/if}
  {#if !showReset}
    <p class="muted">
      <button class="linklike" type="button" onclick={() => (showReset = true)}>
        Forgot password?
      </button>
    </p>
  {:else}
    <p class="muted">
      A workspace admin can generate a reset link for you from their Workspace
      tab. If you run the server yourself, mint one on the server box:
      <code>python -m cloud reset-link your@email</code>
    </p>
  {/if}
  <p class="muted">
    No workspace yet?
    <button class="linklike" type="button" onclick={openCreatePage}>
      Create one on your server →
    </button>
  </p>
  {#if createUrlFallback}
    <p class="muted">
      Couldn't open your browser — visit:
      <input readonly value={createUrlFallback} style="width:100%" onfocus={(e) => e.target.select()} />
    </p>
  {/if}
</div>

<style>
  .login-card {
    max-width: 420px;
    margin: 10vh auto 0;
  }
  .login-card.embedded {
    max-width: none;
    margin: 0;
  }
  .linklike {
    background: none;
    border: none;
    padding: 0;
    color: inherit;
    text-decoration: underline;
    cursor: pointer;
    font: inherit;
  }
  .server-line {
    font-size: 0.85em;
  }
  .server-url {
    font-family: var(--font-mono);
  }
</style>
