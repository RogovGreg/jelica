import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

import { nextSelectIndex, type SelectNavigationKey } from "./select";

export type JelicaSelectOption<Value extends string> = Readonly<{
  value: Value;
  label: string;
  icon?: ReactNode;
}>;

type JelicaSelectProps<Value extends string> = Readonly<{
  label: string;
  value: Value;
  options: readonly JelicaSelectOption<Value>[];
  onValueChange: (value: Value) => void;
  disabled?: boolean;
}>;

const NAVIGATION_KEYS = new Set<string>(["ArrowDown", "ArrowUp", "Home", "End"]);

export function JelicaSelect<Value extends string>({
  label,
  value,
  options,
  onValueChange,
  disabled = false,
}: JelicaSelectProps<Value>) {
  const [open, setOpen] = useState(false);
  const [opensAbove, setOpensAbove] = useState(false);
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const labelId = useId();
  const valueId = useId();
  const listboxId = useId();
  const selectedOption = options[selectedIndex];

  useEffect(() => {
    if (!open) {
      return;
    }

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  useEffect(() => {
    if (open) {
      optionRefs.current[activeIndex]?.focus();
    }
  }, [activeIndex, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const placeMenu = () => {
      const rootBounds = rootRef.current?.getBoundingClientRect();
      const menuBounds = menuRef.current?.getBoundingClientRect();
      if (!rootBounds || !menuBounds) {
        return;
      }
      const spaceBelow = window.innerHeight - rootBounds.bottom;
      setOpensAbove(menuBounds.height > spaceBelow && rootBounds.top > spaceBelow);
    };
    placeMenu();
    window.addEventListener("resize", placeMenu);
    return () => window.removeEventListener("resize", placeMenu);
  }, [open]);

  const openMenu = (index = selectedIndex) => {
    if (!disabled && options.length) {
      setActiveIndex(index);
      setOpensAbove(false);
      setOpen(true);
    }
  };

  const closeMenu = (restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) {
      triggerRef.current?.focus();
    }
  };

  const selectOption = (index: number) => {
    const option = options[index];
    if (!option) {
      return;
    }
    onValueChange(option.value);
    closeMenu(true);
  };

  const handleTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === "Enter" || event.key === " " || NAVIGATION_KEYS.has(event.key)) {
      event.preventDefault();
      const index = NAVIGATION_KEYS.has(event.key)
        ? nextSelectIndex(selectedIndex, options.length, event.key as SelectNavigationKey)
        : selectedIndex;
      openMenu(index);
    }
  };

  const handleListboxKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (NAVIGATION_KEYS.has(event.key)) {
      event.preventDefault();
      setActiveIndex((index) =>
        nextSelectIndex(index, options.length, event.key as SelectNavigationKey),
      );
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectOption(activeIndex);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key === "Tab") {
      closeMenu(true);
    }
  };

  return (
    <div className="jelica-select-field" ref={rootRef}>
      <span className="jelica-select-label" id={labelId}>
        {label}
      </span>
      <button
        ref={triggerRef}
        type="button"
        className={`jelica-select-trigger${selectedOption?.icon ? " has-icon" : ""}`}
        aria-labelledby={`${labelId} ${valueId}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        disabled={disabled}
        onClick={() => (open ? closeMenu(false) : openMenu())}
        onKeyDown={handleTriggerKeyDown}
      >
        {selectedOption?.icon ? (
          <span className="jelica-select-icon" aria-hidden="true">
            {selectedOption.icon}
          </span>
        ) : null}
        <span className="jelica-select-value" id={valueId}>
          {selectedOption?.label ?? value}
        </span>
        <svg className="jelica-select-chevron" viewBox="0 0 16 16" aria-hidden="true">
          <path d="m4 6 4 4 4-4" />
        </svg>
      </button>
      {open ? (
        <div
          ref={menuRef}
          id={listboxId}
          className={`jelica-select-menu${opensAbove ? " opens-above" : ""}`}
          role="listbox"
          aria-labelledby={labelId}
          onKeyDown={handleListboxKeyDown}
        >
          {options.map((option, index) => {
            const selected = option.value === value;
            return (
              <button
                key={option.value}
                ref={(element) => {
                  optionRefs.current[index] = element;
                }}
                type="button"
                className={`jelica-select-option${option.icon ? " has-icon" : ""}`}
                role="option"
                aria-selected={selected}
                tabIndex={index === activeIndex ? 0 : -1}
                onClick={() => selectOption(index)}
                onFocus={() => setActiveIndex(index)}
                onPointerMove={() => setActiveIndex(index)}
              >
                {option.icon ? (
                  <span className="jelica-select-icon" aria-hidden="true">
                    {option.icon}
                  </span>
                ) : null}
                <span>{option.label}</span>
                <svg className="jelica-select-check" viewBox="0 0 16 16" aria-hidden="true">
                  <path d="m3 8 3 3 7-7" />
                </svg>
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
