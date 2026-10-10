/* Native Harness components consume their own semantic tokens. Thoth supplies
 * the palette when embedded; standalone Harness keeps its own preferences. */
(() => {
  if (window.parent === window || window.parent.location.origin !== location.origin) return;
  const root = window.parent.document.documentElement;
  const palette = document.createElement('style');
  palette.id = 'thoth-harness-theme';
  document.head.append(palette);
  let scheduled = false;
  function apply() {
    scheduled = false;
    const css = getComputedStyle(root);
    const read = (...names) => names.map(name => css.getPropertyValue(name).trim()).find(Boolean);
    const bg = read('--thoth-mood-bg', '--bg');
    const layer1 = read('--bg');
    const panel = read('--panel');
    const text = read('--thoth-mood-text', '--text');
    const muted = read('--muted');
    const accent = read('--thoth-mood-accent', '--accent');
    const border = read('--border');
    const colors = {
      'bg-base': bg, 'bg-layer-1': layer1, 'bg-layer-2': panel, 'bg-layer-3': panel,
      'bg-module-platform': panel, 'bg-overlay': panel,
      'label-primary': text, 'label-primary-dimmed': text,
      'label-secondary': muted, 'label-tertiary': muted, 'label-caption': muted,
      'border-l1': border, 'border-l2': border, 'border-l3': border,
      'brand-primary': accent, 'brand-text': accent,
      'button-floating-fill': panel, 'button-elevated-fill': panel,
      'interactive-bg-hover': 'color-mix(in srgb, ' + text + ' 7%, transparent)',
      'interactive-bg-active': 'color-mix(in srgb, ' + accent + ' 12%, transparent)',
      'scrollbar-bg-l1': border, 'scrollbar-bg-l2': border,
      'scrollbar-hover-l1': muted, 'scrollbar-hover-l2': muted,
    };
    const tokens = Object.entries(colors).filter(([,value]) => value && CSS.supports('color', value))
      .map(([name,value]) => '--dsw-alias-' + name + ':' + value + '!important;').join('');
    palette.textContent = 'html,body,body[data-ds-dark-theme]{' + tokens + '--dsw-specific-menu:' + panel + '!important;--dsh-boot-bg:' + bg + '!important;background-color:' + bg + '!important;color-scheme:dark;}';
  }
  function refresh() { if (!scheduled) { scheduled = true; requestAnimationFrame(apply); } }
  apply();
  const observer = new MutationObserver(refresh);
  observer.observe(root, {attributes:true});
  // Use the native dark semantic palette for unchanged status/syntax tokens.
  function dark() { if (!document.body.hasAttribute('data-ds-dark-theme')) document.body.setAttribute('data-ds-dark-theme', ''); }
  let bodyObserver;
  document.addEventListener('DOMContentLoaded', () => {
    dark();
    bodyObserver = new MutationObserver(dark);
    bodyObserver.observe(document.body, {attributes:true, attributeFilter:['data-ds-dark-theme']});
  }, {once:true});
  window.addEventListener('pagehide', () => { observer.disconnect(); bodyObserver?.disconnect(); }, {once:true});
})();
