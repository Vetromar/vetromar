// The spatial UI's bottom status line: every meaningful action narrates here
// in lowercase plain language (the greybox handoff's "say" pattern). Last
// write wins — it's a single-line bar, not a log.

let text = $state("");
let kind = $state("info"); // info | error

export const status = {
  get text() {
    return text;
  },
  get kind() {
    return kind;
  },
  say(msg) {
    text = String(msg ?? "").toLowerCase();
    kind = "info";
  },
  sayError(msg) {
    text = String(msg ?? "something went wrong").toLowerCase();
    kind = "error";
  },
  clear() {
    text = "";
    kind = "info";
  },
};
