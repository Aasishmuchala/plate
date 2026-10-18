// Prompt modal (T97): a large textarea for the Edit node's explicit prompt, so a change list can be read and edited
// at length. The text is bound to `params.prompt` by the caller (`value` / `onChange`); Copy puts it on the clipboard,
// Close (the button or Escape) leaves the modal. It owns no state beyond the "copied" note.

import { type ReactElement, useEffect, useRef, useState } from "react";
import { C, E } from "./theme";
import { Button } from "./widgets";

export interface PromptModalProps {
  value: string;
  onChange: (value: string) => void;
  onClose: () => void;
  title?: string;
  placeholder?: string;
}

/** Put `text` on the clipboard; false when the browser has no clipboard API or refuses it. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || !navigator.clipboard) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

export function PromptModal({ value, onChange, onClose, title = "Prompt", placeholder }: PromptModalProps): ReactElement {
  const area = useRef<HTMLTextAreaElement | null>(null);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    area.current?.focus();
  }, []);

  const copy = async (): Promise<void> => {
    const ok = await copyText(value);
    if (!ok) area.current?.select(); // no clipboard API: leave the text selected for Ctrl+C
    setNote(ok ? "copied" : "clipboard unavailable: the text is selected, press Ctrl+C");
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      data-testid="prompt-modal"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          onClose();
        }
      }}
      style={{ position: "fixed", inset: 0, zIndex: 50, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0, 0, 0, 0.6)" }}
    >
      <div style={{ ...E.frame, width: "min(860px, 92vw)", height: "min(640px, 86vh)", borderRadius: 8, border: `1px solid ${C.line}` }}>
        <div style={E.head}>
          <h3 style={E.title}>{title}</h3>
          <span style={E.sub}>{value.length} characters</span>
        </div>
        <textarea
          ref={area}
          style={{ ...E.wide, flex: 1, resize: "none", fontFamily: "inherit", lineHeight: 1.45 }}
          value={value}
          placeholder={placeholder}
          aria-label="prompt text"
          data-testid="prompt-modal-text"
          onChange={(e) => onChange(e.target.value)}
        />
        <div style={E.row}>
          <Button onClick={() => void copy()} testId="prompt-modal-copy">
            Copy
          </Button>
          <Button onClick={onClose} testId="prompt-modal-close">
            Close
          </Button>
          {note ? (
            <span style={E.note} role="status" data-testid="prompt-modal-note">
              {note}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}
