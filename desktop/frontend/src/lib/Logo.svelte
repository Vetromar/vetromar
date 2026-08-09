<script>
  let { size = 20 } = $props();
</script>

<!-- The shard. viewBox is measured from the rendered bounding box (the corner
     arcs bulge past their endpoints) — do not recompute it from the path. -->
<svg
  viewBox="32 38 122 141"
  style:height="{size}px"
  style:width="{size * 0.8652}px"
  aria-hidden="true"
>
  <path
    d="M 139.09,133.03 L 91.66,175.77 A 1.73,1.73 0 0 1 88.99,175.31 L 37.3,80.54 A 17.17,17.17 0 0 1 39.84,60.59 L 50.47,49.22 A 25.01,25.01 0 0 1 81.66,44.9 L 133.68,76.31 A 35.47,35.47 0 0 1 139.09,133.03 Z"
    fill="currentColor"
  />
</svg>

<style>
  svg {
    display: block;
  }
</style>
