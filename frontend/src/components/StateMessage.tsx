import type { ReactNode } from "react";
import styles from "./StateMessage.module.css";

interface Props {
  title: string;
  children?: ReactNode;
  onRetry?: () => void;
  role?: "alert" | "status";
}

/** Пустое состояние или ошибка: заголовок, пояснение, кнопка «Повторить». */
export function StateMessage({ title, children, onRetry, role = "status" }: Props) {
  return (
    <div className={styles.box} role={role}>
      <p className={styles.title}>{title}</p>
      {children && <div className={styles.text}>{children}</div>}
      {onRetry && (
        <button type="button" className="button button-secondary" onClick={onRetry}>
          Повторить
        </button>
      )}
    </div>
  );
}
