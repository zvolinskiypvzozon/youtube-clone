import type { CSSProperties } from "react";
import styles from "./UserText.module.css";

type Tag = "span" | "p" | "h1" | "h2" | "h3" | "div";

interface Props {
  text: string;
  as?: Tag;
  /** Обрезка по числу строк (многоточие); без значения — весь текст. */
  lines?: number;
  className?: string;
  title?: boolean;
}

/**
 * Единственный способ вывести пользовательский текст (названия, описания, комментарии, ники).
 * Только текстовым узлом — без разбора HTML (security.md, день 5); dir="auto" и изоляция
 * направления — RTL-текст не «перетекает» в соседний интерфейс; обрезка переполнения против
 * нагромождения комбинирующих знаков (аудит шага 1 дня 6).
 */
export function UserText({ text, as: Tag = "span", lines, className, title }: Props) {
  const style = lines ? ({ "--lines": lines } as CSSProperties) : undefined;
  const classes = [styles.text, lines ? styles.clamped : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <Tag dir="auto" className={classes} style={style} title={title ? text : undefined}>
      {text}
    </Tag>
  );
}
