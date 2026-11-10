import logoUrl from "../../../backend/img/logo.jpg";

export type Section = "Nueva conciliación" | "Historial" | "Cuentas" | "Administración de usuarios";

interface Props {
  active: Section;
  userName: string;
  role: string;
  isAdministrator: boolean;
  mobileOpen: boolean;
  onChange: (section: Section) => void;
  onCloseMobile: () => void;
}

export default function NavigationSidebar({ active, userName, role, isAdministrator, mobileOpen, onChange, onCloseMobile }: Props) {
  const items: { label: Section; available: boolean }[] = [
    { label: "Cuentas", available: true },
    { label: "Nueva conciliación", available: true },
    { label: "Administración de usuarios", available: isAdministrator },
  ];
  return (
    <>
      {mobileOpen && <button className="sidebar-backdrop" aria-label="Cerrar navegación" onClick={onCloseMobile} />}
      <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`} aria-label="Navegación principal">
        <button className="mobile-sidebar-close quiet-button" onClick={onCloseMobile} aria-label="Cerrar menú">×</button>
        <div className="sidebar-brand"><img src={logoUrl} alt="ContaRed" /><span><strong>ContaRed</strong><small>Conciliación bancaria</small></span></div>
        <nav className="sidebar-nav">
          {items.filter((item) => item.available).map((item) => (
            <button key={item.label} className={`nav-item ${active === item.label ? "active" : ""}`} disabled={!item.available} onClick={() => { onChange(item.label); onCloseMobile(); }} type="button">
              <span>{item.label}</span>{!item.available && <small>Próximamente</small>}
            </button>
          ))}
        </nav>
        <div className="sidebar-user"><strong>{userName}</strong><small>{role}</small></div>
      </aside>
    </>
  );
}
