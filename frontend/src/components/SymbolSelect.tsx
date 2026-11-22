import { useEffect, useId, useRef, useState } from "react";
import { useInstruments } from "../api/hooks";
import type { Exchange, Instrument } from "../api/types";

export function useDebounced<T>(value: T, ms = 250): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

interface Props {
  value: string;
  exchange?: Exchange | "";
  onSelect: (instrument: Instrument) => void;
  label?: string;
  /** keep the typed text after choosing (for multi-select pickers that clear it) */
  clearOnSelect?: boolean;
  placeholder?: string;
}

/** A search box that suggests instruments as you type (symbol or company name), with full keyboard support. */
export function SymbolSelect({ value, exchange = "", onSelect, label = "Symbol", clearOnSelect = false, placeholder = "Search symbol or company" }: Props) {
  const id = useId();
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  const q = useDebounced(text.trim(), 200);
  const { data, isFetching, isError } = useInstruments({ q: q || undefined, exchange, active: true, page_size: 12 }, open);
  const items = data?.items ?? [];

  useEffect(() => setActive(0), [q]);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const choose = (inst: Instrument) => {
    onSelect(inst);
    setText("");
    setOpen(false);
  };

  return (
    <div className="combo" ref={wrapRef}>
      <label className="field" htmlFor={id}>
        {label}
        <input
          id={id}
          className="input"
          role="combobox"
          aria-expanded={open}
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={open && items[active] ? `${id}-opt-${active}` : undefined}
          autoComplete="off"
          placeholder={clearOnSelect ? placeholder : value || placeholder}
          value={open || clearOnSelect ? text : text || value}
          onFocus={() => setOpen(true)}
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setOpen(true);
              setActive((a) => Math.min(a + 1, Math.max(items.length - 1, 0)));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            } else if (e.key === "Enter" && open && items[active]) {
              e.preventDefault();
              choose(items[active]);
            } else if (e.key === "Escape") {
              setOpen(false);
            }
          }}
        />
      </label>
      {open && (
        <ul id={`${id}-list`} role="listbox" aria-label="Matching instruments">
          {isError && <li aria-disabled="true"><small>Could not load instruments.</small></li>}
          {!isError && !items.length && <li aria-disabled="true"><small>{isFetching ? "Searching…" : "No instrument matches."}</small></li>}
          {items.map((inst, i) => (
            <li
              key={`${inst.exchange}:${inst.symbol}`}
              id={`${id}-opt-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(inst)}
              onMouseEnter={() => setActive(i)}
            >
              <span><strong>{inst.symbol}</strong> <small>{inst.name}</small></span>
              <small>{inst.instrument_type === "index" ? "Index" : inst.exchange}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
