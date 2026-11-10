export interface CountryOption { codigo: "SV" | "GT"; nombre: string }
export interface CompanyOption { empresa_id: string; empresa: string }

interface Props {
  title: string;
  countries: CountryOption[];
  companies: CompanyOption[];
  countryCode: "SV" | "GT" | "";
  companyId: string;
  onCountryChange: (country: "SV" | "GT") => void;
  onCompanyChange: (companyId: string) => void;
  onLogout: () => void;
  logoutError: string;
  signingOut: boolean;
  onOpenMobile: () => void;
}

export default function ContentHeader(props: Props) {
  const company = props.companies.find((item) => item.empresa_id === props.companyId);
  return (
    <header className="content-header">
      <div className="header-title">
        <button className="mobile-menu-button" aria-label="Abrir navegación" onClick={props.onOpenMobile}>☰</button>
        <h1>{props.title}</h1>
      </div>
      <div className="header-controls">
        <label className="header-field">País<select value={props.countryCode} onChange={(event) => props.onCountryChange(event.target.value as "SV" | "GT")}>{props.countries.map((country) => <option key={country.codigo} value={country.codigo}>{country.nombre}</option>)}</select></label>
        <div className="header-company"><span>Empresa</span>{props.companies.length === 1 ? <strong>{company?.empresa ?? props.companies[0]?.empresa}</strong> : <select aria-label="Empresa" value={props.companyId} onChange={(event) => props.onCompanyChange(event.target.value)}>{props.companies.map((item) => <option key={item.empresa_id} value={item.empresa_id}>{item.empresa}</option>)}</select>}</div>
        {props.logoutError && <div className="notice error" role="alert"><span>{props.logoutError}</span><button className="retry-button" type="button" onClick={props.onLogout} disabled={props.signingOut}>Reintentar cierre de sesión</button></div>}
        <button className="quiet-button header-logout" onClick={props.onLogout} disabled={props.signingOut} type="button">{props.signingOut ? "Cerrando sesión…" : "Cerrar sesión"}</button>
      </div>
    </header>
  );
}
