export interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: string;
  id?: string;
  disabled?: boolean;
  describedBy?: string;
}

/** Square ON/OFF block (role="switch"). */
export function Toggle({ checked, onChange, label, id, disabled, describedBy }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      id={id}
      class="toggle"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    >
      <span>ON</span>
      <span>OFF</span>
    </button>
  );
}
