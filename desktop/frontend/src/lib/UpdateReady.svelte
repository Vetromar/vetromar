<script>
  // "Update ready" corner card — the visible half of the auto-update flow.
  // The download already happened silently; this only offers the relaunch.
  let { version, notes, busy, onRelaunch, onLater } = $props();

  let applying = $state(false);
  let failed = $state(false);

  async function relaunch() {
    applying = true;
    failed = false;
    try {
      await onRelaunch();
      // On success the app exits — nothing to reset.
    } catch (e) {
      // Installing can fail on a translocated (never-moved-to-Applications)
      // install. Point at the manual path instead of erroring loudly.
      console.warn("update install failed:", e);
      failed = true;
      applying = false;
    }
  }
</script>

<div class="update-ready card stack">
  <strong>Update ready</strong>
  <p class="muted body">
    Vetromar {version} has been downloaded. Relaunch to start using it.
  </p>
  {#if notes}
    <p class="muted body">{notes}</p>
  {/if}
  {#if busy}
    <p class="muted body">A job is running — relaunch when it finishes.</p>
  {/if}
  {#if failed}
    <p class="muted body">
      The update could not be applied automatically. Move Vetromar to your
      Applications folder, or reinstall from vetromar.com.
    </p>
  {/if}
  <div class="row" style="gap:8px">
    <button class="primary" disabled={busy || applying} onclick={relaunch}>
      {#if applying}<span class="spinner"></span> Relaunching…{:else}Relaunch{/if}
    </button>
    <button disabled={applying} onclick={() => onLater?.()}>Later</button>
  </div>
</div>

<style>
  .update-ready {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 41; /* above the getting-started card (40), below the tour (60) */
    width: 300px;
  }
  .body {
    margin: 0;
    font-size: 0.92em;
  }
</style>
