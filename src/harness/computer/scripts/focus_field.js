// CDP Runtime.callFunctionOn body, invoked with the target element as `this`: bring it into view,
// focus it, and select whatever it already holds, so a following Input.insertText replaces the
// contents (paste-over-selection) rather than appending. Handles form controls (select) and
// contenteditable regions (a range over its contents).
function () {
  this.scrollIntoView({ block: "center" });
  this.focus();
  if (typeof this.select === "function") {
    this.select();
  } else if (this.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(this);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
}
