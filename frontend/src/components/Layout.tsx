import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ChannelIcon, HomeIcon, LogoIcon, MenuIcon, ShieldIcon, UploadIcon, VideosIcon } from "./Icons";
import { STAFF_ROLES } from "../auth/RequireStaff";
import { SearchBox } from "./SearchBox";
import { UserMenu } from "./UserMenu";
import styles from "./Layout.module.css";

export const APP_NAME = "Видеохостинг";

export function Layout() {
  const { user, meError } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  const nav = [
    { to: "/", label: "Главная", icon: <HomeIcon />, end: true },
    ...(user
      ? [
          { to: `/channel/${encodeURIComponent(user.channel.handle)}`, label: "Мой канал", icon: <ChannelIcon />, end: false },
          { to: "/my/videos", label: "Мои видео", icon: <VideosIcon />, end: false },
          { to: "/upload", label: "Загрузить", icon: <UploadIcon />, end: false },
          // Только интерфейс: права на модерацию проверяет API.
          ...(STAFF_ROLES.has(user.role) ? [{ to: "/moderation", label: "Модерация", icon: <ShieldIcon />, end: false }] : []),
        ]
      : []),
  ];

  return (
    <div className={styles.app}>
      <a className="skip-link" href="#main">
        Перейти к содержимому
      </a>
      <header className={styles.header}>
        <div className={styles.start}>
          <button
            type="button"
            className={`icon-button ${styles.menuButton}`}
            aria-label={menuOpen ? "Скрыть меню" : "Показать меню"}
            aria-expanded={menuOpen}
            aria-controls="sidebar"
            onClick={() => setMenuOpen((v) => !v)}
          >
            <MenuIcon />
          </button>
          <Link to="/" className={styles.logo} aria-label={`${APP_NAME} — на главную`}>
            <LogoIcon />
            <span className={styles.logoText}>{APP_NAME}</span>
          </Link>
        </div>
        <SearchBox />
        <div className={styles.end}>
          {user === undefined && !meError ? null : user ? (
            <UserMenu user={user} />
          ) : (
            <Link to="/login" className="button button-outline">
              Войти
            </Link>
          )}
        </div>
      </header>
      <div className={styles.body}>
        <nav
          id="sidebar"
          className={`${styles.sidebar} ${menuOpen ? styles.sidebarOpen : ""}`}
          aria-label="Основное меню"
        >
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `${styles.navItem} ${isActive ? styles.navActive : ""}`}
              onClick={() => setMenuOpen(false)}
            >
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        {menuOpen && <div className={styles.backdrop} onClick={() => setMenuOpen(false)} aria-hidden="true" />}
        <main id="main" className={styles.main} tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
