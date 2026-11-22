import { useEffect, useRef, useState } from "react";
import { getApiKey, setApiKey } from "../api/client";

/**
 * The API key is only needed for actions that change data (import, processing, saving a backtest, editing instruments).
 * It is kept in this tab's sessionStorage, so it is forgotten when the tab closes, and it is sent only to your own API.
 */
export function ApiKeyDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState(getApiKey());

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setValue(getApiKey());
      dialog.showModal?.();
    }
    if (!open && dialog.open) dialog.close?.();
  }, [open]);

  return (
    <dialog ref={ref} onClose={onClose} aria-labelledby="apikey-title">
      <form
        method="dialog"
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          setApiKey(value.trim());
          onClose();
        }}
      >
        <h2 id="apikey-title">API key</h2>
        <p className="muted" style={{ margin: 0 }}>
          Needed only for actions that change data. Copy the value of <span className="mono">API_KEY</span> from your <span className="mono">.env</span> file.
          If the server has no key set, leave this empty. It is remembered for this browser tab only.
        </p>
        <label className="field">
          Key
          <input className="input" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        </label>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn" onClick={() => { setApiKey(""); setValue(""); onClose(); }}>Clear</button>
          <button type="submit" className="btn btn-primary">Save</button>
        </div>
      </form>
    </dialog>
  );
}
