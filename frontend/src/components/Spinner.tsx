import styles from "./Spinner.module.css";

export function Spinner({ label = "Загрузка…" }: { label?: string }) {
  return (
    <div className={styles.wrap} role="status" aria-live="polite">
      <span className={styles.spinner} aria-hidden="true" />
      <span className="visually-hidden">{label}</span>
    </div>
  );
}
