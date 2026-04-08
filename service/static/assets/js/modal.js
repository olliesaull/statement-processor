/**
 * Reusable modal helpers.
 *
 * Provides appModal.show(modalId) for opening a Bootstrap 5 modal by DOM id.
 * Designed so appModal.alert() and appModal.confirm() can be added later for
 * native dialog replacements without changing the shell.
 */
const appModal = (() => {
  /**
   * Open a Bootstrap 5 modal by its DOM id.
   * @param {string} modalId — id attribute of the modal element
   * @returns {bootstrap.Modal|null} — the Modal instance, or null if not found
   */
  const show = (modalId) => {
    const el = document.getElementById(modalId);
    if (!el) return null;
    const instance = bootstrap.Modal.getOrCreateInstance(el);
    instance.show();
    return instance;
  };

  return { show };
})();

export { appModal };
