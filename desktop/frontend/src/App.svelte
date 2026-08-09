<script>
  import { onMount } from "svelte";
  import { api } from "./api.js";
  import { sourcesJob, captureJob, workspaceJob, downloadJob, startBackgroundWatch } from "./lib/jobs.svelte.js";
  import { updater, startUpdateWatch, relaunchToUpdate } from "./lib/updater.svelte.js";
  import UpdateReady from "./lib/UpdateReady.svelte";
  import Setup from "./lib/Setup.svelte";
  import Capture from "./lib/Capture.svelte";
  import Knowledge from "./lib/Knowledge.svelte";
  import Sources from "./lib/Sources.svelte";
  import Workspace from "./lib/Workspace.svelte";
  import Login from "./lib/Login.svelte";
  import Logo from "./lib/Logo.svelte";
  import Onboarding from "./lib/Onboarding.svelte";
  import GettingStarted from "./lib/GettingStarted.svelte";

  let view = $state("loading"); // loading | login | setup | capture | knowledge | sources | workspace
  let health = $state(null);
  let workspace = $state(null); // GET /api/workspace snapshot
  let loadError = $state(null);
  let onboarding = $state(null); // GET /api/onboarding snapshot
  let tourOpen = $state(false);
  // Session-local: once the tour auto-opened, a mid-session refresh must not
  // reopen it while the tour_done POST is still in flight.
  let tourAutoOpened = false;

  async function refresh({ gotoSetup = false } = {}) {
    try {
      loadError = null;
      health = await api.health();
      // Required sign-in: the gate is a cached session on this device — it
      // never blocks on the network (offline keeps working once signed in).
      workspace = await api.workspaceStatus();
      if (!workspace.signed_in) {
        view = "login";
        return;
      }
      if (gotoSetup) view = "setup";
      else if (view === "loading" || view === "login")
        view = health.ready ? "capture" : "setup";
      await refreshOnboarding();
      maybeOpenTour();
    } catch (e) {
      loadError = e.message;
    }
  }

  async function refreshOnboarding() {
    // Fail-soft: onboarding stays null and nothing onboarding-related renders.
    try {
      onboarding = await api.onboardingStatus();
    } catch {}
  }

  function maybeOpenTour() {
    // First entry into the ready app: the login gate and the first-run Setup
    // pin take precedence over the tour.
    if (tourAutoOpened || tourOpen) return;
    if (!onboarding || onboarding.tour_done) return;
    if (!health?.ready || !inApp) return;
    tourAutoOpened = true;
    tourOpen = true;
  }

  async function closeTour() {
    tourOpen = false;
    try {
      onboarding = await api.onboardingUpdate({ tour_done: true });
    } catch {}
  }

  function tourNavigate(tab) {
    // The user chose to dive in — don't leave a modal over their chosen tab.
    view = tab;
    closeTour();
  }

  async function dismissChecklist() {
    try {
      onboarding = await api.onboardingUpdate({ checklist_dismissed: true });
    } catch {}
  }

  // A finished sync or capture may have just completed a checklist item.
  $effect(() => {
    void sourcesJob.finishedCount;
    void captureJob.finishedCount;
    refreshOnboarding();
  });

  function onSignedIn(status) {
    workspace = status;
    // Stay on "login" so refresh() routes with FRESH health.
    refresh();
  }

  function onWorkspaceStatus(status) {
    // The Workspace tab refreshes its own session snapshot on every visit —
    // adopt it here so the header can't go stale.
    workspace = status;
  }

  function onSignOut() {
    workspace = null;
    view = "login";
  }

  function onSetupDone() {
    view = "capture";
    refresh();
  }

  const inApp = $derived(
    view === "capture" || view === "knowledge" || view === "sources" || view === "workspace"
  );

  // Relaunching mid-job would kill a live sync/capture AND risk the swapped
  // bundle's lazy-loaded files under the still-running sidecar — hold it.
  const anyJobRunning = $derived(
    sourcesJob.running || captureJob.running || workspaceJob.running || downloadJob.running
  );
  const updateBannerOpen = $derived(updater.status === "ready" && !updater.dismissed && inApp);

  onMount(() => {
    refresh();
    startBackgroundWatch();
    startUpdateWatch();
  });
</script>

<header class="app-header">
  <div class="brand"><Logo size={22} /> vetromar</div>
  {#if inApp}
    <nav class="tabs">
      <button class:active={view === "capture"} onclick={() => (view = "capture")}>Capture</button>
      <button class:active={view === "knowledge"} onclick={() => (view = "knowledge")}>Knowledge</button>
      <button class:active={view === "sources"} onclick={() => (view = "sources")}>Sources</button>
      <button class:active={view === "workspace"} onclick={() => (view = "workspace")}>Workspace</button>
    </nav>
  {/if}
  <div class="header-right">
    <!-- Jobs keep running when their tab isn't open — these badges make that
         visible and jump back to the owning tab. -->
    {#if sourcesJob.running && view !== "sources"}
      <button class="activity-badge" onclick={() => (view = "sources")}>
        <span class="spinner"></span> syncing…
      </button>
    {/if}
    {#if workspaceJob.running && view !== "workspace"}
      <button class="activity-badge" onclick={() => (view = "workspace")}>
        <span class="spinner"></span> workspace…
      </button>
    {/if}
    {#if captureJob.running && view !== "capture"}
      <button class="activity-badge" onclick={() => (view = "capture")}>
        {#if captureJob.job?.status === "recording"}
          ● recording…
        {:else}
          <span class="spinner"></span> processing…
        {/if}
      </button>
    {/if}
    {#if downloadJob.running && view !== "setup"}
      <button class="activity-badge" onclick={() => refresh({ gotoSetup: true })}>
        <span class="spinner"></span> downloading models…
      </button>
    {/if}
    {#if updater.status === "ready" && updater.dismissed}
      <button class="activity-badge" onclick={() => updater.reopen()}>update ready</button>
    {/if}
    {#if health && inApp}
      <span
        class="badge"
        style:background="color-mix(in srgb, {health.ready ? 'var(--good)' : 'var(--warn)'} 18%, transparent)"
      >
        {health.ready ? "ready" : "setup needed"}
      </span>
      <button onclick={() => refresh({ gotoSetup: true })}>Settings</button>
    {/if}
  </div>
</header>

<main>
  {#if view === "loading"}
    <div class="card"><span class="spinner"></span> &nbsp;Connecting to the engine…</div>
    {#if loadError}
      <p class="error" style="margin-top:16px">Could not reach the local engine: {loadError}</p>
    {/if}
  {:else if view === "login"}
    <Login serverUrl={workspace?.server_url} onSignedIn={onSignedIn} />
  {:else if view === "setup"}
    <!-- Never pin: extraction setup is not a gate on the rest of the app —
         Knowledge browsing and the Workspace tab work without AI. -->
    <Setup
      {health}
      onDone={onSetupDone}
      onChanged={() => refresh()}
      onBack={() => (view = "capture")}
      onReplayTour={health?.ready
        ? () => {
            view = "capture";
            tourOpen = true;
          }
        : null}
    />
  {:else if view === "capture"}
    <Capture backend={health?.backend} />
  {:else if view === "knowledge"}
    <Knowledge />
  {:else if view === "sources"}
    <Sources {health} signedIn={workspace?.signed_in} />
  {:else if view === "workspace"}
    <Workspace {onSignOut} onStatus={onWorkspaceStatus} />
  {/if}
</main>

{#if tourOpen}
  <Onboarding
    role={workspace?.role}
    workspaceName={workspace?.workspace}
    onClose={closeTour}
    onNavigate={tourNavigate}
  />
{/if}
{#if updateBannerOpen}
  <UpdateReady
    version={updater.version}
    notes={updater.notes}
    busy={anyJobRunning}
    onRelaunch={relaunchToUpdate}
    onLater={() => updater.dismiss()}
  />
{/if}
<!-- The update card takes the corner while open; "Later" gives it back. -->
{#if inApp && onboarding && !onboarding.checklist_dismissed && !tourOpen && !updateBannerOpen}
  <GettingStarted
    status={onboarding}
    isAdmin={workspace?.role === "admin"}
    onNavigate={(t) => (view = t)}
    onReplayTour={() => (tourOpen = true)}
    onDismiss={dismissChecklist}
  />
{/if}
