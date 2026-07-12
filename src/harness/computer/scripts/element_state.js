// CDP Runtime.callFunctionOn body (element as `this`): a localized signature of the element's own
// interactive state — expanded / pressed / selected / checked. Immune to page-wide churn, so an
// in-place toggle, disclosure, or tab selection is detected even when the URL and title do not move.
function () {
  const attribute = (name) => (this.getAttribute ? this.getAttribute(name) : null);
  return [
    attribute("aria-expanded"),
    attribute("aria-pressed"),
    attribute("aria-selected"),
    attribute("aria-checked"),
  ].join("|");
}
