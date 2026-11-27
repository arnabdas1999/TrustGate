import { InvestigationList } from "@/components/InvestigationList";

export default function Home() {
  return (
    <>
      <header className="page-head">
        <div>
          <div className="eyebrow">Incidents</div>
          <h1>Investigations</h1>
          <p className="muted">
            When an AI agent breaks after a Flagsmith release, TrustGate finds the flag change that
            caused it and proposes the narrowest repair. Nothing changes in Flagsmith until you
            approve it.
          </p>
        </div>
      </header>
      <InvestigationList />
    </>
  );
}
