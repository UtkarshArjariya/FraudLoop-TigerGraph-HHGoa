import { AlertCircle, ArrowUpRight, FileSearch, RotateCw } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Verdict } from './types';
export const money = (value: number) => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(value);
export const human = (value: string | null | undefined) => (value || 'Pending').replaceAll('_', ' ');
export const percent = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
export function VerdictChip({verdict}: {verdict: Verdict | null}) { return <span className={`chip ${verdict || 'pending'}`}><span className="dot"/>{human(verdict)}</span>; }
export function RouteBadge({route}: {route: string}) {return <span className={`route route-${route}`}>{route === 'auto' ? 'Auto' : route}</span>;}
export function EmptyState({title, children}: {title: string; children?: ReactNode}) { return <div className="empty-state"><FileSearch size={28} aria-hidden/><h3>{title}</h3><p>{children}</p></div>; }
export function ErrorState({error, retry}: {error: unknown; retry?: () => void}) {return <div className="error-state" role="alert"><AlertCircle size={20} aria-hidden/><div><strong>Unable to load this view</strong><p>{error instanceof Error ? error.message : 'The service is unavailable.'}</p>{retry && <button className="button subtle" onClick={retry}><RotateCw size={14} aria-hidden/>Retry</button>}</div></div>;}
export function Skeleton({rows=5}: {rows?: number}) {return <div className="skeleton-stack" aria-label="Loading" aria-busy="true">{Array.from({length:rows},(_,i)=><div className="skeleton" key={i}/>)}</div>;}
export function PageHeading({title, description, children}: {title:string; description: string; children?: ReactNode}) {return <div className="page-heading"><div><h1>{title}</h1><p>{description}</p></div>{children}</div>;}
export function ExternalHint() {return <span className="inline-flex items-center gap-1">Approval only <ArrowUpRight size={13} aria-hidden/></span>;}
