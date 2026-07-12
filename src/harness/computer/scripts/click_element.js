// CDP Runtime.callFunctionOn body, invoked with the target element as `this`: bring it into
// view and click it in the page, so the action stays inside the page's own JS (no cursor moves).
function () {
  this.scrollIntoView({ block: "center" });
  this.click();
}
