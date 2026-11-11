/* Apply saved appearance before the stylesheet loads to avoid a theme flash. */
const preferences = (() => {
  const root = document.documentElement;
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');
  const read = key => { try { return localStorage.getItem(key); } catch { return null; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch { /* Private browsing can disable storage. */ } };
  let language = read('catpaw.language') === 'zh-CN' ? 'zh-CN' : 'en';
  let manualTheme = read('catpaw.theme');
  if (!['light', 'dark'].includes(manualTheme)) manualTheme = null;
  root.lang = language;
  root.dataset.theme = manualTheme || (systemTheme.matches ? 'dark' : 'light');

  function render() {
    for (const element of document.querySelectorAll('[data-i18n]')) element.textContent = t(element.dataset.i18n);
    for (const attribute of ['title', 'aria-label', 'placeholder']) {
      for (const element of document.querySelectorAll(`[data-i18n-${attribute}]`))
        element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`)));
    }
    for (const button of document.querySelectorAll('[data-language]'))
      button.setAttribute('aria-pressed', String(button.dataset.language === language));
    for (const button of document.querySelectorAll('button[data-theme]'))
      button.setAttribute('aria-pressed', String(button.dataset.theme === root.dataset.theme));
  }
  function setTheme(theme) {
    root.dataset.theme = theme;
    render();
    window.dispatchEvent(new Event('themechange'));
  }
  systemTheme.addEventListener('change', event => { if (!manualTheme) setTheme(event.matches ? 'dark' : 'light'); });
  document.addEventListener('DOMContentLoaded', () => {
    render();
    for (const button of document.querySelectorAll('[data-language]')) button.onclick = () => {
      if (language === button.dataset.language) return;
      language = button.dataset.language;
      root.lang = language;
      save('catpaw.language', language);
      render();
      window.dispatchEvent(new Event('languagechange'));
    };
    for (const button of document.querySelectorAll('button[data-theme]')) button.onclick = () => {
      manualTheme = button.dataset.theme;
      save('catpaw.theme', manualTheme);
      setTheme(manualTheme);
    };
  });
  return {get language() { return language; }};
})();
function t(source) {
  return preferences.language === 'en' ? translations[source] ?? source : source;
}
